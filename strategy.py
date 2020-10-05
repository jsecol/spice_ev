from copy import deepcopy

import events

def class_from_str(strategy_name):
    if strategy_name == 'greedy':
        return Greedy
    else:
        raise Exception('unknown strategy with name {}'.format(strategy_name))


class Strategy():
    """ strategy
    """

    def __init__(self, constants, start_time, interval):
        self.world_state = deepcopy(constants)
        self.world_state.future_events = []
        self.current_time = start_time - interval
        self.interval = interval

    def step(self, event_list=[]):
        self.current_time += self.interval

        self.world_state.future_events += event_list
        self.world_state.future_events.sort(key = lambda ev: ev.start_time)

        while True:
            if len(self.world_state.future_events) == 0:
                break
            elif self.world_state.future_events[0].start_time > self.current_time:
                # ignore future events
                break

            # remove event from list
            ev = self.world_state.future_events.pop(0)

            if type(ev) == events.ExternalLoad:
                connector = self.world_state.grid_connectors[ev.grid_connector_id]
                connector.current_loads[ev.name] = ev.value # not reset after last event
            elif type(ev) == events.GridOperatorSignal:
                connector = self.world_state.grid_connectors[ev.grid_connector_id]
                if ev.cost:
                    # set power cost
                    connector.cost = ev.cost
                # set max power from event
                if connector.max_power:
                    if ev.max_power:
                        connector.cur_max_power = max(connector.max_power, ev.max_power)
                    else:
                        # event max power not set: reset to connector power
                        connector.cur_max_power = connector.max_power
                else:
                    # connector max power not set
                    connector.cur_max_power = ev.max_power

            elif type(ev) == events.VehicleEvent:
                vehicle = self.world_state.vehicles[ev.vehicle_id]
                for k,v in ev.update.items():
                    setattr(vehicle, k, v)
                if ev.event_type == "departure":
                    vehicle.connected_charging_station = None
                elif ev.event_type == "arrival":
                    assert vehicle.connected_charging_station is not None
                    assert hasattr(vehicle, 'soc_delta')
                    vehicle.soc += vehicle.soc_delta
                    delattr(vehicle, 'soc_delta')
                    assert(vehicle.soc >= 0, 'SOC of vehicle {} should not be negative'.format(ev.vehicle_id))


            else:
                raise Exception("Unknown event type: {}".format(ev))

        for name, connector in self.world_state.grid_connectors.items():
            if not connector.cost:
                raise Exception("Warning: Connector {} has no associated costs at {}".format(name, time))


class Greedy(Strategy):
    def __init__(self, constants, start_time, interval):
        super().__init__(constants, start_time, interval)
        self.description = "greedy"
        print(self.description)

    def step(self, event_list=[]):
        super().step(event_list)

        grid_connectors = {name: {'cur_max_power': gc.cur_max_power, 'current_load': sum(gc.current_loads.values())} for name, gc in self.world_state.grid_connectors.items()}
        charging_stations = {}

        for vehicle_id in sorted(self.world_state.vehicles):
            vehicle = self.world_state.vehicles[vehicle_id]
            delta_soc = vehicle.desired_soc - vehicle.soc
            charging_station_id = vehicle.connected_charging_station
            if delta_soc > 0 and charging_station_id:
                charging_station = self.world_state.charging_stations[charging_station_id]
                # vehicle needs loading
                #TODO compute charging losses and use charging curve
                energy_needed = delta_soc / 100 * vehicle.vehicle_type.capacity
                power_needed = energy_needed / (self.interval.total_seconds() / 3600)
                grid_connector = grid_connectors[charging_station.parent]
                gc_power_left = grid_connector['cur_max_power'] - grid_connector['current_load']
                cs_power_left = charging_station.max_power - charging_stations.get(charging_station_id, 0)
                power = min(power_needed, vehicle.vehicle_type.max_charging_power, cs_power_left, gc_power_left)

                assert(power >= 0, 'power should not be negative')
                if power == 0:
                    continue

                if charging_station_id in charging_stations:
                    charging_stations[charging_station_id] += power
                else:
                    charging_stations[charging_station_id] = power

                grid_connectors[charging_station.parent]['current_load'] += power

                energy_kwh = self.interval.total_seconds() / 3600 * power
                soc_delta = 100.0 * energy_kwh / vehicle.vehicle_type.capacity
                vehicle.soc += soc_delta
                print('delta_soc {}, desired_soc {}'.format(delta_soc, vehicle.desired_soc))
                print('energy_needed {}, power {}'.format(energy_needed, power))
                print('SOC {}: {}'.format(vehicle_id, vehicle.soc))
                assert(vehicle.soc <= 100)
                assert(vehicle.soc >= 0)

        #TODO return list of charging commands, +meta info
        return {'current_time': self.current_time, 'commands': charging_stations}
