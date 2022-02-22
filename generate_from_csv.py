#!/usr/bin/env python3

import argparse
import csv
import datetime
import json
from os import path
import random

from src.util import set_options_from_config


def generate_from_csv(args):
    """Generates a scenario JSON from csv rotation schedule of fleets.

    :param args: input arguments
    :type args: argparse.Namespace
    :return: None
    """
    missing = [arg for arg in ["input_file", "output"] if vars(args).get(arg) is None]
    if missing:
        raise SystemExit("The following arguments are required: {}".format(", ".join(missing)))

    random.seed(args.seed)

    interval = datetime.timedelta(minutes=args.interval)
    # read csv input file
    input = csv_to_dict(args.input_file)

    # VEHICLES
    if args.vehicle_types is None:
        args.vehicle_types = "examples/vehicle_types.json"
        print("No definition of vehicle types found, using {}".format(args.vehicle_types))
    ext = args.vehicle_types.split('.')[-1]
    if ext != "json":
        print("File extension mismatch: vehicle type file should be .json")
    with open(args.vehicle_types) as f:
        predefined_vehicle_types = json.load(f)

    for row in input:
        row["vehicle_type"] = row["vehicle_type"] + "-" + row["charging_type"]

    input = add_vehicle_id(input)

    for row in input:
        row["vehicle_type"] = row["vehicle_id"].split('_')[0]

    number_vehicles_per_type = get_number_vehicles_per_vehicle_type(input)
    vehicle_types = {}
    vehicles = {}
    batteries = {}
    charging_stations = {}
    events = {
        "grid_operator_signals": [],
        "external_load": {},
        "energy_feed_in": {},
        "vehicle_events": []
    }
    for vehicle_type in number_vehicles_per_type:
        # update vehicle types with vehicles in input csv
        try:
            vehicle_types.update({vehicle_type: predefined_vehicle_types[vehicle_type]})
        except KeyError:
            print(f"The vehicle type {vehicle_type} defined in the input csv cannot be found in "
                  f"vehicle_types.json. Please check for consistency.")

    for bus_type in number_vehicles_per_type.keys():
        for i in range(1, number_vehicles_per_type[bus_type]+1):
            name = bus_type
            v_name = "{}_{}".format(name, i)
            cs_name = "CS_" + v_name
            # define start conditions
            vehicles[v_name] = {
                "connected_charging_station": None,
                "estimated_time_of_departure": None,
                "desired_soc": args.min_soc,
                "soc": args.min_soc,
                "vehicle_type": name
            }

            t = vehicle_types[name]

            cs_power = max([v[1] for v in t['charging_curve']])
            charging_stations[cs_name] = {
                "max_power": cs_power,
                "min_power": 0.1 * cs_power,
                "parent": "GC1"
            }

            # filter all rides for that bus
            vid_list = []
            [vid_list.append(row) for row in input if (row["vehicle_id"] == v_name)]

            # check if each bus is only used once a day
            list_vehicle_days = [datetime.datetime.strptime(d["arrival_time"], '%Y-%m-%d %H:%M:%S').weekday() for d in vid_list]
            count_v_per_day = {i: list_vehicle_days.count(i) for i in list_vehicle_days}
            if any(v > 1 for v in count_v_per_day.values()):
                print("A vehicle is used for more than one rotation on the same day. Please check "
                      "the column >vehicle_id< in the input csv for consistency.")

            # sort events for their departure time, so that the matching departure time of an
            # arrival event can be read out of the next element in vid_list
            vid_list = sorted(vid_list, key=lambda x: x["departure_time"])
            for index, row in enumerate(vid_list):
                departure_event_in_input = True
                arrival = row["arrival_time"]
                arrival = datetime.datetime.strptime(arrival, '%Y-%m-%d %H:%M:%S')
                try:
                    departure = vid_list[index+1]["departure_time"]
                    departure = datetime.datetime.strptime(departure, '%Y-%m-%d %H:%M:%S')
                    next_arrival = vid_list[index+1]["arrival_time"]
                    next_arrival = datetime.datetime.strptime(next_arrival, '%Y-%m-%d %H:%M:%S')
                except IndexError:
                    departure_event_in_input = False
                    departure = arrival + datetime.timedelta(hours=8)

                events["vehicle_events"].append({
                    "signal_time": arrival.isoformat(),
                    "start_time": arrival.isoformat(),
                    "vehicle_id": v_name,
                    "event_type": "arrival",
                    "update": {
                        "connected_charging_station": "CS_" + v_name,
                        "estimated_time_of_departure": departure.isoformat(),
                        "soc_delta": ((100 - float(row["soc"])) / 100) * (-1),
                    }
                })

                # give warning if desired_soc < soc_delta
                if args.min_soc < ((100 - float(row["soc"])) / 100):
                    print(f"The minimum desired soc of {args.min_soc} is lower than the delta_soc"
                          f" of the next ride.")

                if departure_event_in_input:
                    events["vehicle_events"].append({
                        "signal_time": departure.isoformat(),
                        "start_time": departure.isoformat(),
                        "vehicle_id": v_name,
                        "event_type": "departure",
                        "update": {
                            "estimated_time_of_arrival":  next_arrival.isoformat()
                        }
                    })

    # add stationary battery
    for idx, (capacity, c_rate) in enumerate(args.battery):
        if capacity > 0:
            max_power = c_rate * capacity
        else:
            # unlimited battery: set power directly
            max_power = c_rate
        batteries["BAT{}".format(idx+1)] = {
            "parent": "GC1",
            "capacity": capacity,
            "charging_curve": [[0, max_power], [1, max_power]]
        }
    # save path and options for CSV timeseries
    # all paths are relative to output file
    target_path = path.dirname(args.output)
    times = []
    for row in input:
        times.append(row["departure_time"])
    times.sort()
    start = times[0]
    start = datetime.datetime.strptime(start, '%Y-%m-%d %H:%M:%S')
    stop = start + datetime.timedelta(days=args.days)

    if args.include_ext_load_csv:
        filename = args.include_ext_load_csv
        basename = path.splitext(path.basename(filename))[0]
        options = {
            "csv_file": filename,
            "start_time": start.isoformat(),
            "step_duration_s": 900,  # 15 minutes
            "grid_connector_id": "GC1",
            "column": "energy"
        }
        if args.include_ext_csv_option:
            for key, value in args.include_ext_csv_option:
                if key == "step_duration_s":
                    value = int(value)
                options[key] = value
        events['external_load'][basename] = options
        # check if CSV file exists
        ext_csv_path = path.join(target_path, filename)
        if not path.exists(ext_csv_path):
            print(
                "Warning: external csv file '{}' does not exist yet".format(
                    ext_csv_path))

    if args.include_feed_in_csv:
        filename = args.include_feed_in_csv
        basename = path.splitext(path.basename(filename))[0]
        options = {
            "csv_file": filename,
            "start_time": start.astimezone().replace(microsecond=0).isoformat(),
            "step_duration_s": 3600,  # 60 minutes
            "grid_connector_id": "GC1",
            "column": "energy"
        }
        if args.include_feed_in_csv_option:
            for key, value in args.include_feed_in_csv_option:
                if key == "step_duration_s":
                    value = int(value)
                options[key] = value
        events['energy_feed_in'][basename] = options
        feed_in_path = path.join(target_path, filename)
        if not path.exists(feed_in_path):
            print(
                "Warning: feed-in csv file '{}' does not exist yet".format(
                    feed_in_path))

    if args.include_price_csv:
        filename = args.include_price_csv
        # basename = path.splitext(path.basename(filename))[0]
        options = {
            "csv_file": filename,
            "start_time": start.astimezone().replace(microsecond=0).isoformat(),
            "step_duration_s": 3600,  # 60 minutes
            "grid_connector_id": "GC1",
            "column": "price [ct/kWh]"
        }
        for key, value in args.include_price_csv_option:
            if key == "step_duration_s":
                value = int(value)
            options[key] = value
        events['energy_price_from_csv'] = options
        price_csv_path = path.join(target_path, filename)
        if not path.exists(price_csv_path):
            print("Warning: price csv file '{}' does not exist yet".format(
                price_csv_path))

    daily = datetime.timedelta(days=1)
    # price events
    if not args.include_price_csv:
        now = start - daily
        while now < stop + 2 * daily:
            now += daily
            for v_id, v in vehicles.items():
                if now >= stop:
                    # after end of scenario: keep generating trips, but don't include in scenario
                    continue

            # generate prices for the day
            if now < stop:
                morning = now + datetime.timedelta(hours=6)
                evening_by_month = now + datetime.timedelta(hours=22 - abs(6 - now.month))
                events['grid_operator_signals'] += [{
                    # day (6-evening): 15ct
                    "signal_time": max(start, now - daily).isoformat(),
                    "grid_connector_id": "GC1",
                    "start_time": morning.isoformat(),
                    "cost": {
                        "type": "fixed",
                        "value": 0.15 + random.gauss(0, 0.05)
                    }
                }, {
                    # night (depending on month - 6): 5ct
                    "signal_time": max(start, now - daily).isoformat(),
                    "grid_connector_id": "GC1",
                    "start_time": evening_by_month.isoformat(),
                    "cost": {
                        "type": "fixed",
                        "value": 0.05 + random.gauss(0, 0.03)
                    }
                }]
    # create final dict
    j = {
        "scenario": {
            "start_time": start.isoformat(),
            "interval": interval.days * 24 * 60 + interval.seconds // 60,
            "n_intervals": (stop - start) // interval
        },
        "constants": {
            "vehicle_types": vehicle_types,
            "vehicles": vehicles,
            "grid_connectors": {
                "GC1": {
                    "max_power": vars(args).get("gc_power", 530),
                    "cost": {"type": "fixed", "value": 0.3}
                }
            },
            "charging_stations": charging_stations,
            "batteries": batteries
        },
        "events": events
    }

    # Write JSON
    with open(args.output, 'w') as f:
        json.dump(j, f, indent=2)


def get_number_vehicles_per_vehicle_type(dict):
    """
    Evaluates the number of vehicles per vehicle_type from the input csv.

    :param dict: dictionary with all trips as elements
    :type dict: dict
    :return: dictionary {vehicle_type : number of vehicles}
    :rtype: dict
    """

    count_vehicles = {}
    list_vt = []
    for row in dict:
        list_vt.append(row["vehicle_type"])
    # count the appearance of each vehicle_type
    count_vt = {i: list_vt.count(i) for i in list_vt}

    # restructure trips to days of the week and count max number of vehicles per day
    count_vehicles = {bus_type: [0] * 7 for bus_type in count_vt.keys()}
    for row in dict:
        weekday = datetime.datetime.strptime(row["arrival_time"], '%Y-%m-%d %H:%M:%S').weekday()

        count_vehicles[row["vehicle_type"]][weekday-1] += 1
    for bus_type in count_vehicles.keys():
        count_vehicles[bus_type] = max(count_vehicles[bus_type])

    return count_vehicles


def csv_to_dict(csv_path):
    """
    Reads csv file and returns a dict with each element representing a trip

    :param csv_path: path to input csv file
    :type csv_path: str
    :return: dictionary
    :rtype: dict
    """

    dict = []
    with open(csv_path, 'r') as file:
        reader = csv.reader(file)
        # set column names using first row
        columns = next(reader)

        # convert csv to json
        for row in reader:
            row_data = {}
            for i in range(len(row)):
                # set key names
                row_key = columns[i].lower()
                # set key/value
                row_data[row_key] = row[i]
            # add data to json store
            dict.append(row_data)
    return dict

def add_vehicle_id(input, safe=True):
    """
    Assigns all rotations to specific vehicles with distinct vehicle_id.
    :param input: schedule of rotations
    :type input: dict
    :param safe: saved input dict as csv
    :type safe: bool
    :return: schedule of rotations
    :rtype: dict
    """
    # list of dicts to dict
    input = {item['rotation_id']: item for item in input}
    from copy import deepcopy

    # sort rotations and add add vehicle_id
    # filter for vehicle_type
    vehicle_types = set(d['vehicle_type'] for d in input.values())
    for vt in vehicle_types:
        vt_line = {k: v for k, v in input.items() if v["vehicle_type"] == vt}
        # sort list of vehicles by departure time
#        depot_stations = set(d['arrival_name'] for d in vt_line.values())
#        for depot in depot_stations:
        bus_number = 0
#        d_line = {k: v for k, v in vt_line.items() if v["arrival_name"] == depot}
        departures = {key: value for key, value in sorted(vt_line.items(),
                      key=lambda x: x[1]['departure_time'])}
        arrivals = {key: value for key, value in sorted(vt_line.items(),
                    key=lambda x: x[1]['arrival_time'])}
        # get the first arrival
        first_arrival_time = arrivals[list(arrivals.keys())[0]]["arrival_time"]
        for rotation in departures.keys():
            # solange erstes fahrzeug noch nicht wieder angekommen ist: hochzählen
            if datetime.datetime.strptime(first_arrival_time, '%Y-%m-%d %H:%M:%S')+datetime.timedelta(hours=6) >= datetime.datetime.strptime(vt_line[rotation]["departure_time"], '%Y-%m-%d %H:%M:%S'):
                bus_number += 1
                input[rotation]["vehicle_id"] = vt + "_" + str(bus_number)
            # sobald erstes Fahreug wieder angekommen ist, alte Fahreugnummer nehmen
            elif datetime.datetime.strptime(arrivals[list(arrivals.keys())[0]]["arrival_time"], '%Y-%m-%d %H:%M:%S') +datetime.timedelta(hours=6) < \
                    datetime.datetime.strptime(vt_line[rotation]["departure_time"], '%Y-%m-%d %H:%M:%S'):
                #try:
                #    a_bus_number = arrivals[list(arrivals.keys())[0]]["vehicle_id"]
                #except:
                #    print("stop")
                arrival_rotation = list(arrivals.keys())[0]
                try:
                    a_bus_number = input[arrival_rotation]["vehicle_id"]
                except:
                    print("stop")
                del arrivals[arrival_rotation]
                input[rotation]["vehicle_id"] = a_bus_number
            else:
                bus_number += 1
                input[rotation]["vehicle_id"] = vt + "_" + str(bus_number)
    if safe:
        dict = deepcopy(input)
        all_rotations = []
        header = []
        for rotation_id, rotation in dict.items():
#            del rotation["trips"]
            if not header:
                for k, v in rotation.items():
                    header.append(k)
            all_rotations.append(rotation)

        with open('examples/input_vehicle_id.csv', 'w', encoding='UTF8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=header)
            writer.writeheader()
            writer.writerows(all_rotations)

    input = list(input.values())
    return input


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Generate scenarios as JSON files for vehicle charging modelling')
    parser.add_argument('input_file', nargs='?',
                        help='input file name (rotations_example_table.csv)')
    parser.add_argument('output', nargs='?', help='output file name (example.json)')
    parser.add_argument('--days', metavar='N', type=int, default=30,
                        help='set duration of scenario as number of days')
    parser.add_argument('--interval', metavar='MIN', type=int, default=15,
                        help='set number of minutes for each timestep (Δt)')
    parser.add_argument('--min-soc', metavar='SOC', type=float, default=0.8,
                        help='set minimum desired SOC (0 - 1) for each charging process')
    parser.add_argument('--battery', '-b', default=[], nargs=2, type=float, action='append',
                        help='add battery with specified capacity in kWh and C-rate \
                        (-1 for variable capacity, second argument is fixed power))')
    parser.add_argument('--seed', default=None, type=int, help='set random seed')
    parser.add_argument('--include-ext-load-csv',
                        help='include CSV for external load. \
                        You may define custom options with --include-ext-csv-option')
    parser.add_argument('--include-ext-csv-option', '-eo', metavar=('KEY', 'VALUE'),
                        nargs=2, action='append',
                        help='append additional argument to external load')
    parser.add_argument('--include-feed-in-csv',
                        help='include CSV for energy feed-in, e.g., local PV. \
                        You may define custom options with --include-feed-in-csv-option')
    parser.add_argument('--include-feed-in-csv-option', '-fo', metavar=('KEY', 'VALUE'),
                        nargs=2, action='append', help='append additional argument to feed-in load')
    parser.add_argument('--include-price-csv',
                        help='include CSV for energy price. \
                        You may define custom options with --include-price-csv-option')
    parser.add_argument('--include-price-csv-option', '-po', metavar=('KEY', 'VALUE'),
                        nargs=2, default=[], action='append',
                        help='append additional argument to price signals')
    parser.add_argument('--config', help='Use config file to set arguments',
                        default='examples/generate_from_csv.cfg')

    args = parser.parse_args()

    set_options_from_config(args, check=False, verbose=False)

    generate_from_csv(args)
