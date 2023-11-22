from copy import deepcopy
from datetime import timedelta
import warnings

import spice_ev.events as events
from spice_ev.strategy import Strategy
from spice_ev.util import clamp_power, dt_within_core_standing_time


class Schedule(Strategy):
    """ Charging according to a given schedule, that depends on the grid situation. """
    def __init__(self, components, start_time, **kwargs):
        allowed_substrats = ["collective", "individual"]
        self.LOAD_STRAT = "collective"

        # only relevant for sub-strategy "collective"
        self.currently_in_core_standing_time = False
        self.overcharge_necessary = False
        # if set, only warn if vehicle not present during core_standing time instead of aborting
        self.warn_core_standing_time = False
        self.ITERATIONS = 12

        super().__init__(components, start_time, **kwargs)

        self.description = "schedule ({})".format(self.LOAD_STRAT)
        self.uses_schedule = True

        assert self.LOAD_STRAT in allowed_substrats, (
            f"Unknown charging strategy: {self.LOAD_STRAT}. "
            f"Possible options: {', '.join(allowed_substrats)}")
        self.sort_key = lambda v: v[0].get_delta_soc() * v[0].battery.capacity

        if self.LOAD_STRAT == "collective":
            assert len(self.world_state.grid_connectors.values()) == 1, (
                    "Only one grid connector allowed for collective sub-strategy")
            assert self.core_standing_time is not None, (
                "Provide core standing times in the generate_schedule.cfg")

    def dt_to_end_of_time_window(self):
        """Return timedelta between now and end of core standing time (resolution: one minute).

        :return: duration
        :rtype: timedelta
        """

        duration = timedelta()
        interval = timedelta(minutes=1)

        while dt_within_core_standing_time(self.current_time + duration, self.core_standing_time):
            duration += interval

        return duration

    def sim_balanced_charging(self, vehicle, dt, max_power, delta_soc=None):
        """ Simulate a balanced charging process for a single vehicle.

        :param vehicle: vehicle to be charged
        :type vehicle: Vehicle
        :param dt: time period remaining until charging process should be completed
        :type dt: timedelta
        :param max_power: maximum power available during current timestep
        :type max_power: numeric
        :param delta_soc: (optional) desired change in SOC until end of timedelta dt. If not
        :param delta_soc: provided, vehicle is charged to its desired_soc.
        :type delta_soc: numeric
        :return: opt_power (optimal charging power for current timestep),
                charged_soc (delta SOC after the timestep if charged with opt_power)
        :rtype: dict

        Note: If charging balanced across entire time period dt would require a
        charging power less than the vehicle or charging station allows for,
        the vehicle charges prefers to charge in the beginning rather than in the end.
        """

        # get vehicle
        cs_id = vehicle.connected_charging_station
        if cs_id is None:
            # not connected
            return None
        # get connected charging station
        cs = self.world_state.charging_stations[cs_id]
        power = 0
        charged_soc = 0
        delta_soc = delta_soc if delta_soc is not None else vehicle.get_delta_soc()

        if delta_soc > self.EPS:
            # vehicle still needs charging: find minimum power needed to reach desired SOC
            min_power = max(vehicle.vehicle_type.min_charging_power, cs.min_power)
            max_power = min(max_power, vehicle.vehicle_type.charging_curve.max_power)
            max_power = clamp_power(max_power, vehicle, cs)
            # time until departure
            old_soc = vehicle.battery.soc
            idx = 0
            safe = False
            # converge to optimal power for the duration
            # at least ITERATIONS cycles
            # must end with slightly too much power used
            # abort if min_power == max_power (converged to solution)
            while (idx < self.ITERATIONS or not safe) and max_power - min_power > self.EPS:
                idx += 1
                # get new power value (binary search: use average)
                power = (max_power + min_power) / 2
                # load whole time with same power
                charged_soc = vehicle.battery.load(dt, target_power=power)["soc_delta"]
                # reset SOC
                vehicle.battery.soc = old_soc

                if delta_soc - charged_soc > self.EPS:  # charged_soc < delta_soc
                    # power not enough
                    safe = False
                    min_power = power
                else:  # charged_soc >= delta_soc:
                    # power too high or just right (maybe possible with less power)
                    safe = True
                    max_power = power

        return {"opt_power": power, "charged_soc": charged_soc}

    def collect_future_gc_info(self, dt=timedelta(days=1)):
        """ Get grid connector info for each future timestep until all vehicles left.

        :param dt: time period (default: 24 h)
        :type dt: timedelta
        :return: grid connector info
        :rtype: dict
        """

        gc = list(self.world_state.grid_connectors.values())[0]

        gc_info = [{
            "current_loads": {},
            "target": gc.target,
            "charge": False
        }]

        # peek into future events for fixed loads, local generation and schedule
        event_idx = 0
        cur_time = self.current_time - self.interval
        timesteps = dt // self.interval
        for timestep_idx in range(timesteps):
            cur_time += self.interval

            if timestep_idx > 0:
                # copy last GC info
                gc_info.append(deepcopy(gc_info[-1]))

            # get approximation of fixed load
            gc_info[-1]["current_loads"]["fixed_load"] = gc.get_avg_fixed_load(cur_time,
                                                                               self.interval)
            # peek into future events for fixed load or cost changes
            while True:
                try:
                    event = self.world_state.future_events[event_idx]
                except IndexError:
                    # no more events
                    break
                if event.start_time > cur_time:
                    # not this timestep
                    break
                # event handled: don't handle again, so increase index
                event_idx += 1
                if type(event) is events.GridOperatorSignal:
                    # update GC info
                    gc_info[-1]["target"] = \
                        event.target if event.target is not None else gc_info[-1]["target"]
                    gc_info[-1]["charge"] = \
                        event.window if event.window is not None else gc_info[-1]["charge"]
                elif type(event) is events.LocalEnergyGeneration:
                    gc_info[-1]["current_loads"][event.name] = -event.value
                # ignore vehicle events, use vehicle data directly
                # ignore local generation for now as well
            # end of useful events peek into future events for fixed loads, schedule
        return gc_info

    def evaluate_core_standing_time_ahead(self):
        """ Evaluate provided energy per timestep by schedule and needed energy in total.

        | This function is called once at the beginning of each core standing time.
        | Shortcomings from schedule are detected early on and battery energy is allocated to help.

        :raises Exception: if any vehicle is not available during core standing time
            (use *warn_core_standing_time* to suppress)
        """

        # get time parameters of next core standing time
        dt_to_end_core_standing_time = self.dt_to_end_of_time_window()
        TS_to_end_core_standing_time = dt_to_end_core_standing_time // self.interval

        # collect forecasts for all timesteps in this standing time
        gc_infos = self.collect_future_gc_info(dt_to_end_core_standing_time)
        self.power_for_vehicles_per_TS = [
            gc_info.get("target") - sum(gc_info["current_loads"].values())
            for gc_info in gc_infos
        ]
        self.charge_window = [x > 0 for x in self.power_for_vehicles_per_TS]
        TS_to_charge_vehicles = sum(self.charge_window)

        # power from local generation and grid power available for vehicles
        self.energy_available_for_vehicles_on_schedule = sum([
            power / self.ts_per_hour
            for power in self.power_for_vehicles_per_TS if power > self.EPS
        ])

        # How much energy does each vehicle need
        self.energy_needed_per_vehicle = {}
        total_energy_needed_vehicles = 0
        for vehicle_id, vehicle in self.world_state.vehicles.items():
            delta_soc = vehicle.get_delta_soc()
            self.energy_needed_per_vehicle[vehicle_id] = (delta_soc
                                                          * vehicle.battery.capacity
                                                          / vehicle.battery.efficiency
                                                          if delta_soc > self.EPS else 0)
            total_energy_needed_vehicles += self.energy_needed_per_vehicle[vehicle_id]

        # estimate how much energy needs to be charged outside of schedule
        # due to charging properties of vehicles
        self.extra_energy_per_vehicle = {}
        for vehicle_id, vehicle in self.world_state.vehicles.items():
            cs_id = vehicle.connected_charging_station
            if cs_id is None:
                # vehicle not present during core_standing time
                if self.warn_core_standing_time:
                    warnings.warn("{}: vehicle {} not available during core standing time".format(
                        self.current_time, vehicle_id))
                    continue
                else:
                    raise Exception(f"Vehicle {vehicle_id} not available during core standing time")

            cs = self.world_state.charging_stations[cs_id]
            max_charging_power = min(vehicle.vehicle_type.charging_curve.max_power, cs.max_power)
            old_soc = vehicle.battery.soc
            vehicle.battery.load(
                timedelta=TS_to_charge_vehicles * self.interval,
                max_power=max_charging_power, target_soc=vehicle.desired_soc)
            delta_soc = vehicle.get_delta_soc()
            self.extra_energy_per_vehicle[vehicle_id] = delta_soc if delta_soc > self.EPS else 0
            vehicle.battery.soc = old_soc

        missing_energy = (total_energy_needed_vehicles -
                          self.energy_available_for_vehicles_on_schedule)
        # do we need to use our batteries to charge vehicles?
        if missing_energy > self.EPS:
            total_energy_batteries = 0
            for bat in self.world_state.batteries.values():
                total_energy_batteries += (bat.soc * bat.capacity) * bat.efficiency
            bat_energy_for_vehicles = min(missing_energy, total_energy_batteries)
        else:
            bat_energy_for_vehicles = 0
        self.bat_power_for_vehicles = (bat_energy_for_vehicles * self.ts_per_hour
                                       / TS_to_end_core_standing_time)

        self.currently_in_core_standing_time = True

    def charge_vehicles_during_core_standing_time(self):
        """ Charge vehicles during core standing time.

        1. In case no energy was allocated for vehicles in this timestep, only
        those vehicles get to charge that are expected not to meet their goal at the end
        of the core standing time. In this case the schedule is ignored in favor of fully charged
        vehicles.

        2. If the schedule provides enough energy to charge vehicles, vehicles are charged as
        balanced as possible with a higher priority on meeting the schedule requirements.

        :return: charging commands
        :rtype: dict
        """

        charging_stations = {}
        dt_to_end_core_standing_time = self.dt_to_end_of_time_window()

        TS_to_charge_vehicles = sum([1 for power in self.power_for_vehicles_per_TS
                                     if power > self.EPS])
        power_to_charge_vehicles = self.power_for_vehicles_per_TS.pop(0)

        if power_to_charge_vehicles < self.EPS:
            # charge vehicles in excess of schedule
            # only vehicles expected to fall short of desired SOC goal if charging
            # on schedule only are considered
            dt = dt_to_end_core_standing_time - TS_to_charge_vehicles * self.interval
            for vehicle_id, delta_soc in self.extra_energy_per_vehicle.items():
                vehicle = self.world_state.vehicles[vehicle_id]
                cs_id = vehicle.connected_charging_station
                if cs_id is None:
                    # vehicle not present during core_standing_time
                    continue
                # get connected charging station, GC
                cs = self.world_state.charging_stations[cs_id]
                gc = self.world_state.grid_connectors[cs.parent]
                # find optimal power for charging
                power = self.sim_balanced_charging(
                    vehicle, dt, vehicle.vehicle_type.charging_curve.max_power,
                    delta_soc=delta_soc)["opt_power"]
                # load with power
                avg_power, charged_soc = vehicle.battery.load(
                    self.interval, target_power=power).values()
                self.extra_energy_per_vehicle[vehicle_id] -= charged_soc
                charging_stations[cs_id] = cs.current_power = gc.add_load(cs_id, avg_power)

        else:
            # charge according to schedule
            try:
                # fraction of energy distributed in this timestep
                fraction = (power_to_charge_vehicles / self.ts_per_hour
                            / self.energy_available_for_vehicles_on_schedule)
            except ZeroDivisionError:
                # energy available for charging vehicles might be zero
                fraction = 0

            extra_power = 0
            vehicles = sorted(self.energy_needed_per_vehicle.items(), key=lambda i: i[1])
            n_vehicles = len(vehicles)

            gc = list(self.world_state.grid_connectors.values())[0]
            # reality check: Can the batteries provide as much energy as we expect them to?
            total_bat_power_remaining = sum(
                [b.get_available_power(self.interval) for b in self.world_state.batteries.values()]
                ) / self.ts_per_hour
            available_bat_power_for_current_TS = min(
                self.bat_power_for_vehicles, total_bat_power_remaining)
            remaining_power_on_schedule = (gc.target - gc.get_current_load()
                                           + available_bat_power_for_current_TS)
            # iteration counter to determine whether each vehicle got a chance to charge
            i = 0
            while len(vehicles) > 0:
                i += 1
                vehicle_id, energy_needed = vehicles.pop(0)
                vehicle = self.world_state.vehicles[vehicle_id]

                # get connected charging station
                cs_id = vehicle.connected_charging_station
                if cs_id is None:
                    # vehicle not present during core standing time
                    continue
                cs = self.world_state.charging_stations[cs_id]

                #  boundaries of charging process
                power_alloc_for_vehicle = fraction * energy_needed * self.ts_per_hour + extra_power
                # clamp allocated power to possible ranges
                power = min(remaining_power_on_schedule, power_alloc_for_vehicle)
                power = clamp_power(power, vehicle, cs)

                # load with power
                avg_power, charged_soc = vehicle.battery.load(
                    self.interval, target_power=power).values()
                charging_stations[cs_id] = cs.current_power = gc.add_load(cs_id, avg_power)
                remaining_power_on_schedule -= avg_power
                if remaining_power_on_schedule < self.EPS:
                    break

                # can the active charging station bear minimum load?
                assert cs.max_power >= cs.current_power - self.EPS, (
                    "{} - {} over maximum load ({} > {})".format(
                        self.current_time, cs_id, cs.current_power, cs.max_power))

                # pass on unused allocated power to next vehicle
                extra_power = max(power_alloc_for_vehicle - avg_power, 0)
                # once every vehicle had a chance to charge and there is no
                # extra power to be distributed, stop charging process
                if i >= n_vehicles and extra_power < self.EPS:
                    break

                # vehicle didn't get to charge, going to the back of the line
                # allocated power + extra power might be enough to charge in
                # a second try.
                if (cs.max_power - cs.current_power > self.EPS and
                        remaining_power_on_schedule >= cs.min_power and
                        remaining_power_on_schedule >= vehicle.vehicle_type.min_charging_power and
                        vehicle.get_delta_soc() > self.EPS):
                    vehicles.append((vehicle_id, energy_needed))

        # last timestep of core standing time
        if dt_to_end_core_standing_time <= self.interval:
            # In case not all vehicles are satisfied, allow
            # charging over schedule outside of core standing time
            if not all([v.desired_soc - v.battery.soc < self.EPS
                        for v in self.world_state.vehicles.values()]):
                self.overcharge_necessary = True

            self.currently_in_core_standing_time = False

        return charging_stations

    def charge_vehicles_during_core_standing_time_v2g(self, commands):
        if not commands:
            commands = {}
        gc = list(self.world_state.grid_connectors.values())[0]
        # get all vehicles that are connected in this step and order vehicles
        vehicles = sorted([(v, id) for id, v in self.world_state.vehicles.items()
                           if (v.connected_charging_station is not None)
                           and (v.vehicle_type.v2g)], key=lambda x: x[1])

        # vehicles that will not be able to charge on schedule to desired SoC anyway
        vehicles_with_power_issues = \
            [v for v, p in self.extra_energy_per_vehicle.items() if p > self.EPS]

        # charge now determines goal of current time step: charge vs discharge vehicles
        charge_now = self.charge_window[0]

        for vehicle, vehicle_id in vehicles:
            if vehicle_id in vehicles_with_power_issues:
                # don't use vehicles with charging issues for V2G
                continue
            cs_id = vehicle.connected_charging_station
            cs = self.world_state.charging_stations[cs_id]
            sim_vehicle = deepcopy(vehicle)
            cur_time = self.current_time - self.interval
            max_discharge_power = sim_vehicle.battery.unloading_curve.max_power

            # check if vehicles can be loaded until desired_soc in connected timesteps
            old_soc = vehicle.battery.soc
            connected_timesteps = []
            window_change = 0
            # get connected timesteps and count number of window changes
            window = charge_now
            for w in self.charge_window:
                cur_time += self.interval
                if sim_vehicle.estimated_time_of_departure < cur_time:
                    break
                if w != window:
                    window_change += 1
                    window = not window
                connected_timesteps.append(w)

            # count TS until we switch from current goal (charge/discharge) to the opposite goal
            try:
                duration_current_window = self.charge_window.index((not charge_now))
            except ValueError:
                # no more window changes in current standing time
                duration_current_window = len(self.charge_window)

            if not charge_now and window_change >= 1:
                min_soc = vehicle.vehicle_type.discharge_limit
                max_soc = 1
                while max_soc - min_soc > self.EPS:
                    discharge_limit = (max_soc + min_soc) / 2
                    for charge_TS in connected_timesteps:
                        if charge_TS:
                            power = clamp_power(gc.cur_max_power, sim_vehicle, cs)
                            sim_vehicle.battery.load(self.interval, max_power=power)["avg_power"]
                        else:
                            power = min(cs.max_power, max_discharge_power)
                            sim_vehicle.battery.unload(
                                self.interval, max_power=power, target_soc=discharge_limit
                            )["avg_power"]
                    if sim_vehicle.battery.soc <= sim_vehicle.desired_soc - self.EPS:
                        min_soc = discharge_limit
                    else:
                        max_soc = discharge_limit
                    sim_vehicle.battery.soc = old_soc
            elif not charge_now and not window_change:
                discharge_limit = sim_vehicle.desired_soc

            if not charge_now and sim_vehicle.battery.soc <= discharge_limit:
                continue

            # calculate power to charge / discharge
            min_power = 0
            if charge_now:
                max_power = max(0, gc.target - gc.get_current_load())
            else:
                max_power = max(0, abs(gc.target - gc.get_current_load()))
            max_power = min(cs.max_power, max_power)

            # during the last window always aim for desired SoC no matter if in charge
            # or discharge window
            # In case the current window is not the last in the standing time,
            # discharge to discharge_limit or charge to full capacity.
            if charge_now:
                desired_soc = vehicle.desired_soc if window_change == 0 else 1
            else:
                desired_soc = vehicle.desired_soc if window_change == 0 else discharge_limit

            total_power = 0
            while max_power - min_power > self.EPS:
                total_power = (min_power + max_power) / 2
                sufficiently_charged = sim_vehicle.battery.soc >= desired_soc
                # reset soc
                sim_vehicle.battery.soc = old_soc
                for _ in range(duration_current_window):
                    if total_power > 0:
                        if charge_now:
                            power = clamp_power(total_power, sim_vehicle, cs)
                            sim_vehicle.battery.load(self.interval, max_power=power)["avg_power"]
                        else:
                            power = clamp_power(total_power, sim_vehicle, cs)
                            power = min(power, max_discharge_power)
                            sim_vehicle.battery.unload(
                                self.interval, max_power=power, target_soc=discharge_limit
                            )["avg_power"]
                    if charge_now:
                        if sim_vehicle.battery.soc >= desired_soc:
                            # already charged
                            sufficiently_charged = True
                            break
                    else:
                        if sim_vehicle.battery.soc < discharge_limit + self.EPS:
                            sufficiently_charged = False
                            # already discharged
                            break

                if charge_now:
                    if sufficiently_charged:
                        max_power = total_power
                    else:
                        min_power = total_power
                else:
                    if sufficiently_charged:
                        min_power = total_power
                    else:
                        max_power = total_power

            # apply power
            if charge_now:
                if total_power <= 0:
                    charge = 0
                else:
                    power = clamp_power(total_power, vehicle, cs)
                    charge = vehicle.battery.load(self.interval, max_power=power)["avg_power"]
                commands[cs_id] = gc.add_load(cs_id, charge)
                cs.current_power += charge
            if not charge_now:
                if total_power <= 0:
                    discharge = 0
                else:
                    power = clamp_power(total_power, vehicle, cs)
                    power = min(power, max_discharge_power)
                    discharge = vehicle.battery.unload(
                        self.interval, max_power=power, target_soc=discharge_limit)["avg_power"]
                commands[cs_id] = gc.add_load(cs_id, -discharge)
                cs.current_power -= discharge

        self.charge_window.pop(0)

        return commands

    def charge_vehicles_after_core_standing_time(self, charging_stations):
        """ Charge vehicles balanced between end of core standing time and departure.


        :param charging_stations: Charging_commands previously allocated during this timestep
        :type charging_stations: dict
        :return: updated charging_stations
        :rtype: dict
        """

        gc = list(self.world_state.grid_connectors.values())[0]  # only 1 GC supported

        vehicles = self.world_state.vehicles.values()

        power_needed = []
        for vehicle in vehicles:
            if vehicle.connected_charging_station is None:
                continue
            soc_needed = vehicle.desired_soc - vehicle.battery.soc
            power_needed.append(soc_needed * vehicle.battery.capacity)

        if sum(power_needed) < self.EPS:
            # vehicles fully charged
            self.overcharge_necessary = False
            return charging_stations

        if gc.cur_max_power - gc.get_current_load() < self.EPS:
            # grid connector maxed out
            return charging_stations

        # charge vehicles balanced until departure
        for vehicle in vehicles:
            cs_id = vehicle.connected_charging_station
            if cs_id is None:
                continue
            cs = self.world_state.charging_stations[cs_id]
            time_until_departure = vehicle.estimated_time_of_departure - self.current_time
            power = self.sim_balanced_charging(
                vehicle, time_until_departure,
                gc.cur_max_power - gc.get_current_load()
            )['opt_power']

            power = clamp_power(power, vehicle, cs)
            avg_power = vehicle.battery.load(
                self.interval, max_power=power, target_soc=vehicle.desired_soc)["avg_power"]
            cs_id = vehicle.connected_charging_station
            charging_stations[cs_id] = cs.current_power = gc.add_load(cs_id, avg_power)

        return charging_stations

    def charge_vehicles(self):
        """ Charge vehicles.

        :return: charging_stations (An updated version of the input containing total of all\
            charging commands determined until this point.)
        :rtype: dict
        """

        charging_stations = {}

        vehicles_at_gc = {gc_id: [] for gc_id in self.world_state.grid_connectors.keys()}
        # find vehicles for each grid connector
        for vehicle in self.world_state.vehicles.values():
            cs_id = vehicle.connected_charging_station
            if cs_id is None:
                continue
            cs = self.world_state.charging_stations[cs_id]
            gc_id = cs.parent
            vehicles_at_gc[gc_id].append((vehicle, cs))

        for gc_id, vehicles in vehicles_at_gc.items():
            gc = self.world_state.grid_connectors[gc_id]
            assert gc.target is not None, "No schedule for GC '{}'".format(gc_id)
            vehicles = sorted(vehicles, key=self.sort_key)

            total_power = gc.target - gc.get_current_load()

            power_needed = []
            for vehicle, _ in vehicles:
                soc_needed = vehicle.desired_soc - vehicle.battery.soc
                power_needed.append(soc_needed * vehicle.battery.capacity)

            if total_power < self.EPS or sum(power_needed) < self.EPS:
                # no power scheduled or all vehicles fully charged: skip this GC
                continue

            for vehicle, cs in vehicles:
                # only "collective" sub-strategy allowed here
                # charge vehicles with available power from local generation
                power = max(-gc.get_current_load(), 0)
                power = clamp_power(power, vehicle, cs)
                avg_power = vehicle.battery.load(
                    self.interval, max_power=power, target_soc=vehicle.desired_soc)["avg_power"]
                cs_id = vehicle.connected_charging_station
                charging_stations[cs_id] = cs.current_power = gc.add_load(cs_id, avg_power)

        return charging_stations

    def charge_individually(self):
        charging_stations = {}
        for vid, vehicle in self.world_state.vehicles.items():
            cs_id = vehicle.connected_charging_station
            if cs_id is None:
                # vehicle is not charging: skip
                continue
            cs = self.world_state.charging_stations[cs_id]
            gc_id = cs.parent
            gc = self.world_state.grid_connectors[gc_id]
            if vehicle.schedule is None:
                raise RuntimeError(f"Vehicle {vid} without schedule")

            # look into future events for schedule changes
            cur_schedule = vehicle.schedule
            schedule = []
            event_idx = 0
            cur_time = self.current_time
            charging = vehicle.estimated_time_of_departure is not None
            while charging and cur_time < vehicle.estimated_time_of_departure:
                # peek into future events for schedule changes or departure
                while True:
                    try:
                        event = self.world_state.future_events[event_idx]
                    except IndexError:
                        # no more events
                        charging = False
                        break
                    if event.start_time > cur_time:
                        # not this timestep
                        break
                    # event handled: don't handle again, so increase index
                    event_idx += 1
                    if type(event) is events.VehicleEvent and event.vehicle_id == vid:
                        if event.event_type == 'schedule':
                            cur_schedule = event.update["schedule"]
                        elif event.event_type == 'departure':
                            # usually, for this type signal_time = start_time,
                            # so can't detect it in advance
                            charging = False
                            break
                cur_time += self.interval
                schedule.append(cur_schedule)

            # compute number of remaining charging intervals (off by one)
            if vehicle.estimated_time_of_departure is not None:
                standing = (vehicle.estimated_time_of_departure-self.current_time) // self.interval
            else:
                # no info about departure: don't allocate additional power
                standing = None
            # charge according to schedule, see if target_soc can be reached
            old_soc = vehicle.battery.soc
            gc_power_left = gc.cur_max_power - gc.get_current_load()
            for s in schedule:
                power = clamp_power(s, vehicle, cs)
                vehicle.battery.load(self.interval, target_power=power)

            if standing is None or standing > len(schedule):
                # not entire schedule known / standing longer than current schedule:
                # don't allocate additional power (avoid power creep)
                add_power = 0
            elif vehicle.get_delta_soc() < self.EPS:
                # schedule is sufficient to reach desired soc: no additional power needed
                add_power = 0
            elif gc_power_left < self.EPS:
                # GC max power reached
                add_power = 0
            elif not schedule:
                # empty schedule -> already leaving
                add_power = 0
            else:
                # schedule not sufficient: add same amount of power to every timestep
                min_power = 0
                max_power = cs.max_power
                while max_power-min_power > self.EPS:
                    add_power = (max_power + min_power) / 2
                    vehicle.battery.soc = old_soc
                    for s in schedule:
                        power = clamp_power(s + add_power, vehicle, cs)
                        vehicle.battery.load(self.interval, target_power=power)
                    if vehicle.get_delta_soc() < self.EPS:
                        max_power = add_power
                    else:
                        min_power = add_power

            vehicle.battery.soc = old_soc
            # charge for real
            power = clamp_power(vehicle.schedule + add_power, vehicle, cs)
            # don't exceed GC max power
            power = min(power, gc_power_left)
            avg_power = vehicle.battery.load(self.interval, target_power=power)["avg_power"]
            charging_stations[cs_id] = cs.current_power = gc.add_load(cs_id, avg_power)
        return charging_stations

    def utilize_stationary_batteries(self):
        """ Adjust deviation with batteries. """
        for bid, battery in self.world_state.batteries.items():
            gc_id = battery.parent
            gc = self.world_state.grid_connectors[gc_id]
            if gc.target is None:
                # no schedule set
                continue
            # get difference between target and GC load
            current_load = gc.get_current_load()
            needed_power = gc.target - current_load
            # get remaining available positive and negative GC power
            avail_pos_power = gc.cur_max_power - current_load
            avail_neg_power = gc.cur_max_power + current_load

            # try to reach target power
            if needed_power < -self.EPS:
                # too much power drawn: support with battery
                power = -needed_power
                # don't exceed GC limit
                power = min(power, avail_neg_power)
                bat_power = -battery.unload(self.interval, target_power=power)["avg_power"]
            elif needed_power > self.EPS:
                # not enough power drawn: charge battery
                power = needed_power
                # don't exceed GC limit
                power = min(power, avail_pos_power)
                if power < battery.min_charging_power:
                    power = 0
                bat_power = battery.load(self.interval, max_power=power)["avg_power"]
            else:
                # target already reached
                bat_power = 0

            gc.add_load(bid, bat_power)

    def step(self):
        """ Calculate charging power in each timestep.

        :return: current time and commands of the charging stations
        :rtype: dict
        """

        # no vehicle is charging at beginning of TS
        for cs in self.world_state.charging_stations.values():
            cs.current_power = 0

        charging_stations = {}

        if self.LOAD_STRAT == "collective":
            if dt_within_core_standing_time(self.current_time, self.core_standing_time):
                # only run in first TS of core standing time
                if not self.currently_in_core_standing_time:
                    self.evaluate_core_standing_time_ahead()
                charging_stations = self.charge_vehicles_during_core_standing_time()
                if any([v.vehicle_type.v2g for v in self.world_state.vehicles.values()]):
                    self.charge_vehicles_during_core_standing_time_v2g(charging_stations)
            else:
                # charge excess power from local generation greedy outside of core standing time ON
                # schedule
                charging_stations = self.charge_vehicles()
                # any vehicle below desired SoC after core standing time?
                # charge balanced OFF schedule
                if self.overcharge_necessary:
                    charging_stations = self.charge_vehicles_after_core_standing_time(
                        charging_stations)
        elif self.LOAD_STRAT == "individual":
            charging_stations = self.charge_individually()

        # always try to charge/discharge stationary batteries
        self.utilize_stationary_batteries()

        return {'current_time': self.current_time, 'commands': charging_stations}
