# SpiceEV - Simulation Program for Individual Charging Events of Electric Vehicles 

A tool to generate scenarios of electric-vehicle fleets and simulate different charging
strategies.

# Documentation

Full documentation can be found [here](https://spice_ev.readthedocs.io/en/latest/)

## Installation

Just clone this repository. This tool just has an optional dependency on
Matplotlib. Everything else uses the Python (>= 3.6) standard library.

## Examples

Generate a scenario and store it in a JSON file:

```sh
./generate.py example.json
```

Generate a 7-day scenario with 10 cars of different types and 15 minute timesteps:

```sh
./generate.py --days 7 --cars 6 golf --cars 4 sprinter --interval 15 example.json
```

Run a simulation of this scenario using the `greedy` charging strategy and show
plots of the results:

```sh
./simulate.py example.json --strategy greedy --visual
```

Generate a timeseries of an energy price:
```sh
./generate_energy_price.py price.csv
```

Include this energy price in scenario:
```sh
./generate.py --include-price-csv price.csv example.json
```
Please note that included file paths are relative to the scenario file location. Consider this directory structure:

Calculate and include schedule:
```sh
./generate_fixed_schedule_for_scenario.py --scenario example.json --input data/timeseries/NSM_1.csv --output data/schedules/NSM_1.csv
```
Please note that included file paths are relative to the scenario file location. Consider this directory structure:

```sh
├── scenarios
│   ├── price
│   │   ├── price.csv
│   ├── my_scenario
│   │   ├── external_load.csv
│   │   ├── example.json
```

To include the price and external load timeseries:
```sh
./generate.py --include-price-csv ../price/price.csv --include-ext-load-csv external_load.csv example.json
```

Show all command line options:

```sh
./generate -h
./simulate.py -h
```

There are also example configuration files in the example folder. The required input/output must still be specified manually:

```sh
./generate.py --config examples/generate.cfg examples/example.json
./simulate.py --config examples/simulate.cfg examples/example.json
```

## SimBEV integration

This tools supports scenarios generated by the [SimBEV](https://github.com/rl-institut/simbev) tool. Convert SimBEV output files to a SpiceEV scenario: 
```sh
generate_from_simbev.py --simbev /path/to/simbev/output/ example.json
```

# License

SpiceEV is licensed under the MIT License as described in the file [LICENSE](https://github.com/rl-institut/spice_ev/blob/dev/LICENSE)
