#!/usr/bin/env python3
import csv
import json
import datetime
import argparse

from src import util

# constants for grid fee

# constant utilization time of the grid needed in order to find commodity and capacity charge
# in the price sheet:
UTILIZATION_TIME_PER_YEAR_EC = 2500  # ec: edge condition, [h/a]

# maximum yearly energy supply for SLP customers:
MAX_ENERGY_SUPPLY_PER_YEAR_SLP = 100000


def read_simulation_csv(csv_file):
    """
    Reads prices, power values and charging signals for each timestamp from  simulation results

    :param csv_file: csv file with simulation results
    :type csv_file: str
    :return: timestamps, prices, power supplied from the grid, power fed into the grid, needed power
    of fixed load, charging signals
    :rtype: dict of lists
    """

    timestamps_list = []
    price_list = []  # [€/kWh]
    power_grid_supply_list = []  # [kW]
    power_fix_load_list = []  # [kW]
    charging_signal_list = []  # [-]
    with open(csv_file, "r", newline="") as simulation_data:
        reader = csv.DictReader(simulation_data, delimiter=",")
        for row in reader:

            # find values for parameter
            timestamp = datetime.datetime.fromisoformat(row["time"])
            price = float(row.get("price [EUR/kWh]", 0))
            power_grid_supply = float(row["grid power [kW]"])
            power_fix_load = float(row["ext.load [kW]"])

            # append value to the respective list:
            timestamps_list.append(timestamp)
            price_list.append(price)
            power_grid_supply_list.append(power_grid_supply)
            power_fix_load_list.append(power_fix_load)

            try:
                charging_signal = bool(row["window"])
            except KeyError:
                charging_signal = None
            charging_signal_list.append(charging_signal)

    return {
        "timestamps_list": timestamps_list,
        "price_list": price_list,
        "power_grid_supply_list": power_grid_supply_list,
        "power_fix_load_list": power_fix_load_list,
        "charging_signal_list": charging_signal_list,
    }


def get_flexible_load(power_grid_supply_list, power_fix_load_list):
    """Determines power of flexible load

    :param power_grid_supply_list: power supplied from the power grid
    :type power_grid_supply_list: list
    :param power_fix_load_list: power of the fixed load
    :type power_fix_load_list: list
    :return: power of flexible load in kW
    :rtype: list
    """

    return [power_grid_supply_list[i] - power_fix_load_list[i]
            for i in range(len(power_grid_supply_list))]


def find_prices(price_sheet_path, strategy, voltage_level, utilization_time_per_year,
                energy_supply_per_year, utilization_time_per_year_ec=UTILIZATION_TIME_PER_YEAR_EC):
    """Reads commodity and capacity charge from price sheets.

    For type 'SLP' the capacity charge is equivalent to the basic charge.

    :param price_sheet_path: path of price sheet
    :type price_sheet_path: str
    :param strategy: charging strategy for the electric vehicles
    :type strategy: str
    :param voltage_level: voltage level of the power grid
    :type voltage_level: str
    :param utilization_time_per_year: utilization time of the power grid per year
    :type utilization_time_per_year: int
    :param energy_supply_per_year: total energy supply from the power grid per year
    :type energy_supply_per_year: float
    :param utilization_time_per_year_ec: minimum value of the utilization time per year in order to
    use the right column of the price sheet (ec: edge condition)
    :type utilization_time_per_year_ec: int
    :return: commodity charge, capacity charge, fee type
    :rtype: float, float, str
    """

    with open(price_sheet_path, "r", newline="") as ps:
        price_sheet = json.load(ps)
    if (strategy == "greedy" or strategy == "balanced") \
            and abs(energy_supply_per_year) <= MAX_ENERGY_SUPPLY_PER_YEAR_SLP:
        # customer type 'SLP'
        fee_type = "SLP"
        commodity_charge = price_sheet["grid_fee"]["SLP"]["commodity_charge_ct/kWh"]["net_price"]
        capacity_charge = price_sheet["grid_fee"]["SLP"]["basic_charge_EUR/a"]["net_price"]
    elif utilization_time_per_year < utilization_time_per_year_ec:
        # customer type 'RLM' with utilization_time_per_year < utilization_time_per_year_ec
        fee_type = "RLM"
        commodity_charge = price_sheet["grid_fee"]["RLM"][
            "<" + str(utilization_time_per_year_ec) + "_h/a"][
            "commodity_charge_ct/kWh"][voltage_level]
        capacity_charge = price_sheet["grid_fee"]["RLM"][
            "<" + str(utilization_time_per_year_ec) + "_h/a"][
            "capacity_charge_EUR/kW*a"][voltage_level]
    else:
        # customer type 'RLM' with utilization_time_per_year >= utilization_time_per_year_ec
        fee_type = "RLM"
        commodity_charge = price_sheet["grid_fee"]["RLM"][
            ">=" + str(utilization_time_per_year_ec) + "_h/a"][
            "commodity_charge_ct/kWh"][voltage_level]
        capacity_charge = price_sheet["grid_fee"]["RLM"][
            ">=" + str(utilization_time_per_year_ec) + "_h/a"][
            "capacity_charge_EUR/kW*a"][voltage_level]

    return commodity_charge, capacity_charge, fee_type


def calculate_commodity_costs(price_list, power_grid_supply_list, interval, fraction_year):
    """Calculates commodity costs for all types of customers

    :param price_list: price list with commodity charge per timestamp
    :type price_list: list
    :param power_grid_supply_list: power supplied from the grid
    :type power_grid_supply_list: list
    :param interval: simulation interval
    :type interval: timedelta
    :param fraction_year: simulation time relative to one year
    :type fraction_year: float
    :return: commodity costs per year and simulation period in Euro
    :rtype: float
    """

    # start value for commodity costs (variable gets updated with every timestep)
    commodity_costs_eur_sim = 0

    # create lists with energy supply per timestep and calculate costs:
    # factor 3600: kilo Joule --> kWh
    # factor 100: ct --> €
    for i in range(len(power_grid_supply_list)):
        energy_supply_per_timestep = \
            power_grid_supply_list[i] * interval.total_seconds() / 3600  # [kWh]
        commodity_costs_eur_sim = commodity_costs_eur_sim + \
            (energy_supply_per_timestep * price_list[i] / 100)  # [€]
    commodity_costs_eur_per_year = commodity_costs_eur_sim / fraction_year

    return commodity_costs_eur_per_year, commodity_costs_eur_sim


def calculate_capacity_costs_rlm(capacity_charge, max_power_strategy):
    """Calculates the capacity costs per year and simulation period for RLM customers

    :param capacity_charge: capacity charge from price sheet
    :type capacity_charge: float
    :param max_power_strategy: power for the calculation of the capacity costs
    :type max_power_strategy: float
    :return: capacity costs per year in Euro
    :rtype: float
    """

    return capacity_charge * max_power_strategy  # [€]


def calculate_costs(strategy, voltage_level, interval,
                    timestamps_list, power_grid_supply_list,
                    price_list, power_fix_load_list, charging_signal_list,
                    core_standing_time_dict, price_sheet_json, results_json=None,
                    power_pv_nominal=0):
    """Calculate costs for the chosen charging strategy

    :param strategy: charging strategy
    :type strategy: str
    :param voltage_level: voltage level of the power grid the fleet is connected to
    :type voltage_level: str
    :param interval: duration of one simulation timestep
    :type interval: timedelta
    :param timestamps_list: timestamps from simulation
    :type timestamps_list: list
    :param power_grid_supply_list: power supplied from the grid
    :type power_grid_supply_list: list
    :param price_list: prices for energy supply in EUR/kWh
    :type price_list: list
    :param power_fix_load_list: power supplied from the grid for the fixed load
    :type power_fix_load_list list
    :param charging_signal_list: charging signal given by the distribution system operator
    (1: charge, 0: don't charge)
    :type charging_signal_list: list
    :param core_standing_time_dict: defined core standing time of the fleet
    :type core_standing_time_dict: dict
    :param price_sheet_json: path to price sheet
    :type price_sheet_json: str
    :param results_json: path to resulting json
    :type results_json: str
    :param power_pv_nominal: nominal power of pv power plant
    :type power_pv_nominal: int
    :return: total costs per year and simulation period (fees and taxes included)
    :rtype: float
    """

    # sanity checks
    assert voltage_level is not None, "Voltage level must be set for cost calculation"
    assert price_sheet_json is not None, "Price sheet must be given"

    # PRICE SHEET
    with open(price_sheet_json, "r", newline="") as ps:
        price_sheet = json.load(ps)

    # TEMPORAL PARAMETERS
    # fraction of scenario duration in relation to one year
    fraction_year = len(timestamps_list) * interval / datetime.timedelta(days=365)

    # split power into feed-in and supply (change sign)
    power_feed_in_list = [max(v, 0) for v in power_grid_supply_list]
    power_grid_supply_list = [max(-v, 0) for v in power_grid_supply_list]

    # ENERGY SUPPLY:
    energy_supply_sim = sum(power_grid_supply_list) * interval.total_seconds() / 3600
    energy_supply_per_year = energy_supply_sim / fraction_year

    # COSTS FROM COMMODITY AND CAPACITY CHARGE DEPENDING ON CHARGING STRATEGY:
    if strategy in ["greedy", "balanced", "distributed"]:
        """
        Calculates costs in accordance with existing payment models.
        For SLP customers the variable capacity_charge is equivalent to the basic charge
        """

        # maximum power supplied from the grid:
        max_power_grid_supply = max(power_grid_supply_list)

        # prices:
        if max_power_grid_supply == 0:
            utilization_time_per_year = 0
        else:
            utilization_time_per_year = abs(energy_supply_per_year / max_power_grid_supply)  # [h/a]
        commodity_charge, capacity_charge, fee_type = find_prices(
            price_sheet_json,
            strategy,
            voltage_level,
            utilization_time_per_year,
            energy_supply_per_year,
            UTILIZATION_TIME_PER_YEAR_EC
        )

        # CAPACITY COSTS:
        if fee_type == "SLP":
            capacity_costs_eur = capacity_charge

        else:  # RLM
            capacity_costs_eur = calculate_capacity_costs_rlm(
                capacity_charge, max_power_grid_supply)

        # COMMODITY COSTS:
        price_list = [commodity_charge] * len(power_grid_supply_list)
        commodity_costs_eur_per_year, commodity_costs_eur_sim = calculate_commodity_costs(
            price_list, power_grid_supply_list, interval, fraction_year)

    elif strategy == "balanced_market":
        """Payment model for the charging strategy 'balanced market'.
        For the charging strategy a price time series is used. The fixed and flexible load are
        charged separately.
        Commodity and capacity costs fixed: The price is depending on the utilization time per year
        (as usual). For the utilization time the maximum fixed load and the fixed energy supply per
        year is used. Then the fixed costs are calculated as usual.
        Commodity and capacity costs flexible: For the flexible load all prices are based on the
        prices for a utilization time <2500 hours in the price sheet (prices for grid friendly power
        supply). For the commodity charge the generated price time series is adjusted to the prices
        for a utilization time >=2500 hours. Then the flexible commodity costs are calculated as
        usual. The flexible capacity costs are calculated only for grid supply in the high tariff
        window.
        """

        # COSTS FOR FIXED LOAD

        # maximum fixed power supplied from the grid [kW]:
        max_power_grid_supply_fix = max(power_fix_load_list)

        if max_power_grid_supply_fix == 0:  # no fix load existing
            commodity_costs_eur_per_year_fix = 0
            commodity_costs_eur_sim_fix = 0
            capacity_costs_eur_fix = 0
        else:  # fixed load existing
            # fixed energy supply:
            energy_supply_sim_fix = sum(power_fix_load_list) * interval.total_seconds() / 3600
            energy_supply_per_year_fix = energy_supply_sim_fix / fraction_year

            # prices:
            utilization_time_per_year_fix = abs(
                energy_supply_per_year_fix / max_power_grid_supply_fix)  # [h/a]
            commodity_charge_fix, capacity_charge_fix, fee_type = \
                find_prices(price_sheet_json, strategy, voltage_level,
                            utilization_time_per_year_fix, energy_supply_per_year_fix,
                            UTILIZATION_TIME_PER_YEAR_EC)

            # commodity costs for fixed load:
            price_list_fix_load = [commodity_charge_fix] * len(power_fix_load_list)
            commodity_costs_eur_per_year_fix, commodity_costs_eur_sim_fix = \
                calculate_commodity_costs(price_list_fix_load, power_fix_load_list,
                                          interval, fraction_year)

            # capacity costs for fixed load:
            capacity_costs_eur_fix = calculate_capacity_costs_rlm(
                capacity_charge_fix, max_power_grid_supply_fix)

        # COSTS FOR FLEXIBLE LOAD

        power_flex_load_list = get_flexible_load(power_grid_supply_list, power_fix_load_list)

        # commodity charge used for comparison of tariffs (comp: compare):
        # The price time series for the flexible load in balanced market is based on the left
        # column of the price sheet. Consequently the prices from this column are needed for
        # the comparison of the tariffs.
        # set utilization_time for prices in left column
        utilization_time_per_year_comp = (UTILIZATION_TIME_PER_YEAR_EC - 1)
        commodity_charge_comp, capacity_charge_comp, fee_type_comp = \
            find_prices(price_sheet_json, strategy, voltage_level, utilization_time_per_year_comp,
                        energy_supply_per_year, UTILIZATION_TIME_PER_YEAR_EC)

        # adjust given price list (EUR/kWh --> ct/kWh)
        price_list_ct = [price * 100 for price in price_list]

        # low and medium tariff
        commodity_charge_lt = commodity_charge_comp * price_sheet[
            "strategy_related_cost_parameters"]["balanced_market"][
            "low_tariff_factor"]  # low tariff [€/kWh]
        commodity_charge_mt = commodity_charge_comp * price_sheet[
            "strategy_related_cost_parameters"][
            "balanced_market"]["medium_tariff_factor"]  # medium tariff [€/kWh]

        # find power at times of high tariff
        max_power_costs = 0
        for i, power in enumerate(power_flex_load_list):
            if price_list_ct[i] <= 0:
                continue
            if price_list_ct[i] in [commodity_charge_lt, commodity_charge_mt]:
                # different tariff
                continue
            # find maximum power
            if power > max_power_costs:
                max_power_costs = power
        # TODO: what happens when there are no times at high tariff?

        # capacity costs for flexible load:
        # set a suitable utilization time in order to use prices for grid friendly charging
        utilization_time_per_year = UTILIZATION_TIME_PER_YEAR_EC
        commodity_charge_flex, capacity_charge_flex, fee_type = \
            find_prices(price_sheet_json, strategy, voltage_level, utilization_time_per_year,
                        energy_supply_per_year, UTILIZATION_TIME_PER_YEAR_EC)
        capacity_costs_eur_flex = \
            calculate_capacity_costs_rlm(capacity_charge_flex, max_power_costs)

        # price list for commodity charge for flexible load [ct/kWh]
        ratio_commodity_charge = commodity_charge_flex / commodity_charge_comp
        price_list_cc = [price * ratio_commodity_charge for price in price_list_ct]

        # commodity costs for flexible load:
        commodity_costs_eur_per_year_flex, commodity_costs_eur_sim_flex = calculate_commodity_costs(
            price_list_cc, power_grid_supply_list, interval, fraction_year)

        # TOTAl COSTS:
        commodity_costs_eur_sim = commodity_costs_eur_sim_fix + commodity_costs_eur_sim_flex
        commodity_costs_eur_per_year = (commodity_costs_eur_per_year_fix
                                        + commodity_costs_eur_per_year_flex)
        capacity_costs_eur = capacity_costs_eur_fix + capacity_costs_eur_flex

    elif strategy == "flex_window":
        """Payment model for the charging strategy 'flex window'.
        The charging strategy uses a charging signal time series (1 = charge, 0 = don't charge).
        The fixed and flexible load are charged separately.
        Commodity and capacity costs fixed: The price is depending on the utilization time per year
        (as usual). For the
        utilization time the maximum fixed load and the fixed energy supply per year is used. Then
        the fixed costs are calculated as usual.
        Commodity and capacity costs flexible: For the flexible load all prices are based on the
        right column of the price sheet (prices for grid friendly power supply). Then the flexible
        commodity costs are calculated as usual. The flexible capacity costs are calculated only for
        grid supply in the high tariff window (signal = 0).
        """

        # COSTS FOR FIXED LOAD

        # maximum fixed power supplied from the grid [kW]
        max_power_grid_supply_fix = max(power_fix_load_list)

        if max_power_grid_supply_fix == 0:
            # no fix load existing
            commodity_costs_eur_per_year_fix = 0
            commodity_costs_eur_sim_fix = 0
            capacity_costs_eur_fix = 0
        else:
            # fixed load existing
            # fixed energy supply:
            energy_supply_sim_fix = sum(power_fix_load_list) * interval.total_seconds() / 3600
            energy_supply_per_year_fix = energy_supply_sim_fix / fraction_year

            # prices:
            utilization_time_per_year_fix = abs(
                energy_supply_per_year_fix / max_power_grid_supply_fix)  # [h/a]
            commodity_charge_fix, capacity_charge_fix, fee_type = \
                find_prices(price_sheet_json, strategy, voltage_level,
                            utilization_time_per_year_fix, energy_supply_per_year_fix,
                            UTILIZATION_TIME_PER_YEAR_EC)

            # commodity costs for fixed load:
            price_list_fix_load = [commodity_charge_fix] * len(power_fix_load_list)
            commodity_costs_eur_per_year_fix, commodity_costs_eur_sim_fix = \
                calculate_commodity_costs(price_list_fix_load, power_fix_load_list,
                                          interval, fraction_year)

            # capacity costs for fixed load:
            capacity_costs_eur_fix = \
                calculate_capacity_costs_rlm(capacity_charge_fix, max_power_grid_supply_fix)

        # COSTS FOR FLEXIBLE LOAD

        power_flex_load_list = get_flexible_load(power_grid_supply_list, power_fix_load_list)

        # prices:
        # set a suitable utilization time in order to use prices for grid friendly charging
        utilization_time_per_year_flex = UTILIZATION_TIME_PER_YEAR_EC
        commodity_charge_flex, capacity_charge_flex, fee_type = \
            find_prices(price_sheet_json, strategy, voltage_level, utilization_time_per_year_flex,
                        energy_supply_per_year, UTILIZATION_TIME_PER_YEAR_EC)

        # commodity costs for flexible load:
        price_list_flex_load = [commodity_charge_flex] * len(power_flex_load_list)
        commodity_costs_eur_per_year_flex, commodity_costs_eur_sim_flex = \
            calculate_commodity_costs(price_list_flex_load, power_flex_load_list,
                                      interval, fraction_year)

        # capacity costs for flexible load:
        power_flex_load_window_list = []
        for i in range(len(power_flex_load_list)):
            if not charging_signal_list[i] and power_flex_load_list[i] < 0:
                power_flex_load_window_list.append(power_flex_load_list[i])
        # no flexible capacity costs if charging takes place only when signal = 1
        if power_flex_load_window_list == []:
            capacity_costs_eur_flex = 0
        else:
            max_power_grid_supply_flex = min(power_flex_load_window_list)
            capacity_costs_eur_flex = \
                calculate_capacity_costs_rlm(capacity_charge_flex, max_power_grid_supply_flex)

        # TOTAl COSTS:
        commodity_costs_eur_sim = commodity_costs_eur_sim_fix + commodity_costs_eur_sim_flex
        commodity_costs_eur_per_year = (commodity_costs_eur_per_year_fix
                                        + commodity_costs_eur_per_year_flex)
        capacity_costs_eur = capacity_costs_eur_fix + capacity_costs_eur_flex

    elif strategy == "schedule":
        """Payment model for the charging strategy 'schedule'.
        For the charging strategy a core standing time is chosen in which the distribution system
        operator can choose how the GC should draw power. The fixed and flexible load are charged
        separately.
        Commodity and capacity costs fixed: The price is depending on the utilization time per year
        (as usual). For the utilization time the maximum fixed load and the energy supply per year
        for the fixed load is used. The commodity charge can be lowered by a flat fee in order to
        reimburse flexibility. Then the fixed commodity and capacity costs are calculated as usual.
        Commodity and capacity costs flexible: For the flexible load all prices are based on the
        right column of the price sheet (prices for grid friendly power supply). Then the flexible
        commodity costs are calculated as usual. The capacity costs for the flexible load are
        calculated for the times outside of the core standing time only.
        """

        # COSTS FOR FIXED LOAD

        # maximum fixed power supplied from the grid:
        max_power_grid_supply_fix = max(power_fix_load_list)

        if max_power_grid_supply_fix == 0:
            # no fixed load existing
            commodity_costs_eur_per_year_fix = 0
            commodity_costs_eur_sim_fix = 0
            capacity_costs_eur_fix = 0
        else:
            # fixed load existing
            # fixed energy supply:
            energy_supply_sim_fix = sum(power_fix_load_list) * interval.total_seconds() / 3600
            energy_supply_per_year_fix = energy_supply_sim_fix / fraction_year

            # prices
            utilization_time_per_year_fix = abs(
                energy_supply_per_year_fix / max_power_grid_supply_fix)  # [h/a]
            commodity_charge_fix, capacity_charge_fix, fee_type = \
                find_prices(price_sheet_json, strategy, voltage_level,
                            utilization_time_per_year_fix, energy_supply_per_year_fix,
                            UTILIZATION_TIME_PER_YEAR_EC)
            reduction_commodity_charge = price_sheet["strategy_related_cost_parameters"][
                "schedule"]["reduction_of_commodity_charge"]
            commodity_charge_fix = commodity_charge_fix - reduction_commodity_charge

            # commodity costs for fixed load:
            price_list_fix_load = [commodity_charge_fix, ] * len(power_fix_load_list)
            commodity_costs_eur_per_year_fix, commodity_costs_eur_sim_fix = \
                calculate_commodity_costs(price_list_fix_load, power_fix_load_list,
                                          interval, fraction_year)

            # capacity costs for fixed load:
            max_power_grid_supply_fix = min(power_fix_load_list)
            capacity_costs_eur_fix = calculate_capacity_costs_rlm(
                capacity_charge_fix, max_power_grid_supply_fix)

        # COSTS FOR FLEXIBLE LOAD

        # power of flexible load:
        power_flex_load_list = get_flexible_load(power_grid_supply_list, power_fix_load_list)

        # prices:
        # set a suitable utilization time in order to use prices for grid friendly charging
        utilization_time_per_year_flex = UTILIZATION_TIME_PER_YEAR_EC
        commodity_charge_flex, capacity_charge_flex, fee_type = \
            find_prices(price_sheet_json, strategy, voltage_level, utilization_time_per_year_flex,
                        energy_supply_per_year, UTILIZATION_TIME_PER_YEAR_EC)

        # commodity costs for flexible load:
        price_list_flex_load = [commodity_charge_flex] * len(power_flex_load_list)
        commodity_costs_eur_per_year_flex, commodity_costs_eur_sim_flex = \
            calculate_commodity_costs(price_list_flex_load, power_flex_load_list,
                                      interval, fraction_year)

        # capacity costs for flexible load:
        power_outside_core_standing_time_flex_list = [
            p for i, p in enumerate(power_flex_load_list) if p < 0 and not
            util.dt_within_core_standing_time(timestamps_list[i], core_standing_time_dict)]

        # charging only within core standing time (cst)
        if power_outside_core_standing_time_flex_list == []:
            max_power_grid_supply_outside_cst_flex = 0
        else:
            max_power_grid_supply_outside_cst_flex = min(power_outside_core_standing_time_flex_list)

        capacity_costs_eur_flex = calculate_capacity_costs_rlm(
            capacity_charge_flex, max_power_grid_supply_outside_cst_flex)

        # TOTAL COSTS:
        commodity_costs_eur_sim = commodity_costs_eur_sim_fix + commodity_costs_eur_sim_flex
        commodity_costs_eur_per_year = (commodity_costs_eur_per_year_fix
                                        + commodity_costs_eur_per_year_flex)
        capacity_costs_eur = capacity_costs_eur_fix + capacity_costs_eur_flex
    else:
        raise Exception(f"Cost calculation does not support charging strategy {str(strategy)}")

    # COSTS NOT RELATED TO STRATEGIES

    # ADDITIONAL COSTS FOR RLM-CONSUMERS:
    if fee_type == "RLM":
        additional_costs_per_year = price_sheet["grid_fee"]["RLM"]["additional_costs"]["costs"]
        additional_costs_sim = additional_costs_per_year * fraction_year
    else:
        additional_costs_per_year = 0
        additional_costs_sim = 0

    # COSTS FOR POWER PROCUREMENT:
    power_procurement_charge = price_sheet["power_procurement"]["charge"]  # [ct/kWh]
    power_procurement_costs_sim = (power_procurement_charge * energy_supply_sim / 100)  # [EUR]
    power_procurement_costs_per_year = (power_procurement_costs_sim / fraction_year)  # [EUR]

    # COSTS FROM LEVIES:

    # prices:
    eeg_levy = price_sheet["levies"]["EEG-levy"]  # [ct/kWh]
    chp_levy = price_sheet["levies"]["chp_levy"]  # [ct/kWh], chp: combined heat and power
    individual_charge_levy = price_sheet["levies"]["individual_charge_levy"]  # [ct/kWh]
    offshore_levy = price_sheet["levies"]["offshore_levy"]  # [ct/kWh]
    interruptible_loads_levy = price_sheet["levies"]["interruptible_loads_levy"]  # [ct/kWh]

    # costs for simulation_period:
    eeg_costs_sim = eeg_levy * energy_supply_sim / 100  # [EUR]
    chp_costs_sim = chp_levy * energy_supply_sim / 100  # [EUR]
    individual_charge_costs_sim = (individual_charge_levy * energy_supply_sim / 100)  # [EUR]
    offshore_costs_sim = offshore_levy * energy_supply_sim / 100  # [EUR]
    interruptible_loads_costs_sim = (interruptible_loads_levy * energy_supply_sim / 100)  # [EUR]
    levies_costs_total_sim = (
            eeg_costs_sim
            + chp_costs_sim
            + individual_charge_costs_sim
            + offshore_costs_sim
            + interruptible_loads_costs_sim
    )

    # costs per year:
    eeg_costs_per_year = eeg_costs_sim / fraction_year  # [EUR]
    chp_costs_per_year = chp_costs_sim / fraction_year  # [EUR]
    individual_charge_costs_per_year = individual_charge_costs_sim / fraction_year  # [EUR]
    offshore_costs_per_year = offshore_costs_sim / fraction_year  # [EUR]
    interruptible_loads_costs_per_year = interruptible_loads_costs_sim / fraction_year  # [EUR]

    # COSTS FROM CONCESSION FEE:

    concession_fee = price_sheet["concession_fee"]["charge"]  # [ct/kWh]
    concession_fee_costs_sim = concession_fee * energy_supply_sim / 100  # [EUR]
    concession_fee_costs_per_year = concession_fee_costs_sim / fraction_year  # [EUR]

    # COSTS FROM FEED-IN REMUNERATION:

    # get nominal power of pv power plant:

    # PV power plant existing:
    if power_pv_nominal != 0:
        # find charge for PV remuneration depending on nominal power pf PV plant:
        pv_nominal_power_max = price_sheet["feed-in_remuneration"]["PV"]["kWp"]
        pv_remuneration = price_sheet["feed-in_remuneration"]["PV"]["remuneration"]
        if power_pv_nominal <= pv_nominal_power_max[0]:
            feed_in_charge_pv = pv_remuneration[0]  # [ct/kWh]
        elif power_pv_nominal <= pv_nominal_power_max[1]:
            feed_in_charge_pv = pv_remuneration[1]  # [ct/kWh]
        elif power_pv_nominal <= pv_nominal_power_max[2]:
            feed_in_charge_pv = pv_remuneration[2]  # [ct/kWh]
        else:
            raise ValueError("Feed-in remuneration for PV is calculated only for nominal powers of "
                             f"up to {pv_nominal_power_max[-1]} kWp")
    # PV power plant not existing:
    else:
        feed_in_charge_pv = 0  # [ct/kWh]

    # energy feed in by PV power plant:
    energy_feed_in_pv_sim = sum(power_feed_in_list) * interval.total_seconds() / 3600  # [kWh]
    energy_feed_in_pv_per_year = energy_feed_in_pv_sim / fraction_year  # [kWh]

    # costs for PV feed-in:
    pv_feed_in_costs_sim = energy_feed_in_pv_sim * feed_in_charge_pv / 100  # [EUR]
    pv_feed_in_costs_per_year = energy_feed_in_pv_per_year * feed_in_charge_pv / 100  # [EUR]

    # COSTS FROM TAXES AND TOTAL COSTS:

    # electricity tax:
    electricity_tax = price_sheet["taxes"]["tax_on_electricity"]  # [ct/kWh]
    electricity_tax_costs_sim = electricity_tax * energy_supply_sim / 100  # [EUR]
    electricity_tax_costs_per_year = electricity_tax_costs_sim / fraction_year  # [EUR]

    # value added tax:
    value_added_tax_percent = price_sheet["taxes"]["value_added_tax"]  # [%]
    value_added_tax = value_added_tax_percent / 100  # [-]

    # total costs without value added tax (commodity costs and capacity costs included):
    costs_total_not_value_added_eur_sim = (
            commodity_costs_eur_sim
            + capacity_costs_eur
            + power_procurement_costs_sim
            + additional_costs_sim
            + levies_costs_total_sim
            + concession_fee_costs_sim
            + electricity_tax_costs_sim
    )  # [EUR]

    # total costs for year (capacity costs are always given per year)
    costs_total_not_value_added_eur_per_year = ((costs_total_not_value_added_eur_sim -
                                                 capacity_costs_eur) / fraction_year +
                                                capacity_costs_eur)

    # costs from value added tax:
    value_added_tax_costs_sim = value_added_tax * costs_total_not_value_added_eur_sim
    value_added_tax_costs_per_year = value_added_tax * costs_total_not_value_added_eur_per_year

    # total costs with value added tax (commodity costs and capacity costs included):
    costs_total_value_added_eur_sim = (costs_total_not_value_added_eur_sim
                                       + value_added_tax_costs_sim)
    costs_total_value_added_eur_per_year = (costs_total_not_value_added_eur_per_year
                                            + value_added_tax_costs_per_year)

    # aggregated and rounded values
    round_to_places = 2
    total_costs_sim = round(costs_total_value_added_eur_sim - pv_feed_in_costs_sim, round_to_places)
    total_costs_per_year = round(
        costs_total_value_added_eur_per_year - pv_feed_in_costs_per_year, round_to_places)

    commodity_costs_eur_per_year = round(commodity_costs_eur_per_year, round_to_places)
    capacity_costs_eur = round(capacity_costs_eur, round_to_places)

    power_procurement_costs_sim = round(power_procurement_costs_sim, round_to_places)
    power_procurement_costs_per_year = round(power_procurement_costs_per_year, round_to_places)

    levies_fees_and_taxes_per_year = round(
        round(eeg_costs_per_year, round_to_places) +
        round(chp_costs_per_year, round_to_places) +
        round(individual_charge_costs_per_year, round_to_places) +
        round(offshore_costs_per_year, round_to_places) +
        round(interruptible_loads_costs_per_year, round_to_places) +
        round(concession_fee_costs_per_year, round_to_places) +
        round(electricity_tax_costs_per_year, round_to_places) +
        round(value_added_tax_costs_per_year, round_to_places),
        round_to_places)

    feed_in_remuneration_per_year = round(pv_feed_in_costs_per_year, round_to_places)

    # WRITE ALL COSTS INTO JSON:
    if results_json is not None:

        capacity_or_basic_costs = "capacity costs"

        if strategy in ["greedy", "balanced", "distributed"]:
            # strategies without differentiation between fixed and flexible load
            information_fix_flex = "no differentiation between fixed and flexible load"
            commodity_costs_eur_per_year_fix = information_fix_flex
            commodity_costs_eur_sim_fix = information_fix_flex
            capacity_costs_eur_fix = information_fix_flex
            commodity_costs_eur_per_year_flex = information_fix_flex
            commodity_costs_eur_sim_flex = information_fix_flex
            capacity_costs_eur_flex = information_fix_flex
            if fee_type == 'SLP':
                capacity_or_basic_costs = "basic costs"

        json_results_costs = {"costs": {
            "electricity costs": {
                "per year": {
                    "total (gross)": total_costs_per_year,
                    "grid_fee": {
                        "total grid fee": round(
                            commodity_costs_eur_per_year + capacity_costs_eur, round_to_places),
                        "commodity costs": {
                            "total costs": commodity_costs_eur_per_year,
                            "costs for fixed load": commodity_costs_eur_per_year_fix,
                            "costs for flexible load": commodity_costs_eur_per_year_flex,
                        },
                        "capacity_or_basic_costs": {
                            "total costs": capacity_costs_eur,
                            "costs for fixed load": capacity_costs_eur_fix,
                            "costs for flexible load": capacity_costs_eur_flex,
                        },
                        "additional costs": round(additional_costs_per_year, round_to_places),
                    },
                    "power procurement": power_procurement_costs_per_year,
                    "levies": {
                        "EEG-levy": round(eeg_costs_per_year, round_to_places),
                        "chp levy": round(chp_costs_per_year, round_to_places),
                        "individual charge levy": round(individual_charge_costs_per_year,
                                                        round_to_places),
                        "Offshore levy": round(offshore_costs_per_year, round_to_places),
                        "interruptible loads levy": round(interruptible_loads_costs_per_year,
                                                          round_to_places),
                    },
                    "concession fee": round(concession_fee_costs_per_year, round_to_places),
                    "taxes": {
                        "value added tax": round(value_added_tax_costs_per_year, round_to_places),
                        "tax on electricity": round(electricity_tax_costs_per_year, round_to_places)
                    },
                    "feed-in remuneration": {
                        "PV": feed_in_remuneration_per_year,
                        "V2G": "to be implemented"
                    },
                    "unit": "EUR",
                    "info": "energy costs for one year",
                },
                "for simulation period": {
                    "total (gross)": total_costs_sim,
                    "grid fee": {
                        "total grid fee": round(commodity_costs_eur_sim
                                                + capacity_costs_eur, round_to_places),
                        "commodity costs": {
                            "total costs": round(commodity_costs_eur_sim, round_to_places),
                            "costs for fixed load": commodity_costs_eur_sim_fix,
                            "costs for flexible load": commodity_costs_eur_sim_flex,
                        },
                        capacity_or_basic_costs: {
                            "total costs": round(capacity_costs_eur, round_to_places),
                            "costs for fixed load": capacity_costs_eur_fix,
                            "costs for flexible load": capacity_costs_eur_flex,
                        },
                        "additional costs": round(additional_costs_sim, round_to_places),
                    },
                    "power procurement": power_procurement_costs_sim,
                    "levies": {
                        "EEG-levy": round(eeg_costs_sim, round_to_places),
                        "chp levy": round(chp_costs_sim, round_to_places),
                        "individual charge levy": round(individual_charge_costs_sim,
                                                        round_to_places),
                        "Offshore levy": round(offshore_costs_sim, round_to_places),
                        "interruptible loads levy": round(interruptible_loads_costs_sim,
                                                          round_to_places),
                    },
                    "concession fee": round(concession_fee_costs_sim, round_to_places),
                    "taxes": {
                        "value added tax": round(value_added_tax_costs_sim, round_to_places),
                        "tax on electricity": round(electricity_tax_costs_sim, round_to_places),
                    },
                    "feed-in remuneration": {
                        "PV": round(pv_feed_in_costs_sim, round_to_places),
                        "V2G": "to be implemented"
                    },
                    "unit": "EUR",
                    "info": "energy costs for simulation period",
                },
            }
        }}

        # add dictionary to json with simulation data:
        with open(results_json, "r", newline="") as sj:
            simulation_json = json.load(sj)
        simulation_json.update(json_results_costs)
        with open(results_json, "w", newline="") as sj:
            json.dump(simulation_json, sj, indent=2)

    # OUTPUT FOR SIMULATE.PY:
    return {
        "total_costs_per_year": total_costs_per_year,
        "commodity_costs_eur_per_year": commodity_costs_eur_per_year,
        "capacity_costs_eur": capacity_costs_eur,
        "power_procurement_per_year": power_procurement_costs_per_year,
        "levies_fees_and_taxes_per_year": levies_fees_and_taxes_per_year,
        "feed_in_remuneration_per_year": feed_in_remuneration_per_year,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Generate scenarios as JSON files for vehicle charging modelling')
    parser.add_argument('--voltage-level', '-vl', help='Choose voltage level for cost calculation')
    parser.add_argument('--pv-power', type=int, default=0,
                        help='set nominal power for local photovoltaic power plant in kWp')
    parser.add_argument('--get-timeseries', '-ts', help='get timeseries from csv file.')
    parser.add_argument('--get-results', '-r', help='get simulation results from json file.')
    parser.add_argument('--cost-parameters-file', '-cp', help='get cost parameters from json file.')
    parser.add_argument('--config', help='Use config file to set arguments')

    args = parser.parse_args()

    util.set_options_from_config(args, check=False, verbose=False)

    # load simulation results:
    with open(args.get_results, "r", newline="") as sj:
        simulation_json = json.load(sj)

    # strategy:
    strategy = simulation_json.get("charging_strategy", {}).get("strategy")
    assert strategy is not None, "Charging strategy not set in results file"

    # simulation interval in minutes:
    interval_min = simulation_json.get("temporal_parameters", {}).get("interval")
    assert interval_min is not None, "Simulation interval length not set in results file"

    # core standing time for fleet:
    core_standing_time_dict = simulation_json.get("core_standing_time")

    # load simulation time series:
    timeseries_lists = read_simulation_csv(args.get_timeseries)

    # voltage level of grid connection:
    voltage_level = args.voltage_level or simulation_json.get("grid_connector",
                                                              {}).get("voltage_level")
    assert voltage_level is not None, "Voltage level is not defined"

    # cost calculation:
    calculate_costs(
        strategy=strategy,
        voltage_level=voltage_level,
        interval=datetime.timedelta(minutes=interval_min),
        core_standing_time_dict=core_standing_time_dict,
        price_sheet_json=args.cost_parameters_file,
        results_json=args.get_results,
        power_pv_nominal=args.pv_power,
        **timeseries_lists
    )