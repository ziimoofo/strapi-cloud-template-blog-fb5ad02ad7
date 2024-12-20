import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from boto3.dynamodb.conditions import Key
from fastapi import Depends
import numpy as np

from optimization_api.helpers.caching.get_redis_cache_client import get_redis_cache_client
from optimization_api.logger import get_logger
from optimization_api.models.status.fleet_status import (
    CurrentLocationModel,
    DriverStatusModel,
    EmptyLocationModel,
    EmptyStatusEnum,
    FleetModel,
    FleetResponseModel,
    HomeLocationModel,
    sort_drivers_by_null_locations,
)
from optimization_api.settings import settings
from shared.auth.models.current_user import CustomerDriver
from shared.models.enums.load_types import LoadTypes
from shared.models.tuples import Address, Coordinates
from shared.persistence.helpers.dynamodb_helper import get_dynamodb_helper
from shared.persistence.models.entities.customer_entity import CustomerEntity
from shared.persistence.models.entities.driver_entity import (
    DriverEntity,
    DriverYard,
    pytz,
)
from shared.persistence.models.entities.schedule_item_entity import ScheduleItemEntity
from shared.persistence.models.entities.truck_location_entity import TruckLocationEntity
from shared.persistence.protocols.customer_service_protocol import CustomerServiceProtocol
from shared.persistence.protocols.drivers_service_protocol import DriversServiceProtocol
from shared.persistence.protocols.dynamodb_helper_protocol import DynamoDBHelperProtocol
from shared.persistence.protocols.schedule_persistence_service_protocol import (
    SchedulePersistenceServiceProtocol,
)
from shared.persistence.services.customer_service import get_customer_service
from shared.persistence.services.drivers_service import get_drivers_service
from shared.persistence.services.schedule_persistence_service import SchedulePersistenceService
from shared.protocols.cache_service_protocol import CacheServiceProtocol
from shared.protocols.logger_protocol import LoggerProtocol
from shared.services.caching.cache_service import CacheService
from shared.services.trimble.trimble import Trimble, TrimbleApi


DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DATE_FORMAT_WITH_TIMEZONE = "%Y-%m-%d %H:%M:%S %z"
STATUS_CACHE_PATTERN = "EMPTY_STATUS_SERVICE:{}"
STATUS_CACHE_PATTERN_WITH_DRIVER = "EMPTY_STATUS_SERVICE:{}+{}"


class EmptyStatusService:
    def __init__(
        self,
        logger: LoggerProtocol,
        dynamodb: DynamoDBHelperProtocol,
        trimble: Trimble = None,
        schedule_service: SchedulePersistenceServiceProtocol = None,
        customer_service: CustomerServiceProtocol = None,
        drivers_service: DriversServiceProtocol = None,
        cache_service: CacheServiceProtocol = None,
    ):
        self.logger = logger
        self.dynamodb = dynamodb
        self.trimble = trimble
        self.schedule_service = schedule_service
        self.customer_service = customer_service
        self.drivers_service = drivers_service
        self.cache_service = cache_service

    def _check_if_driver_is_late(self, customer_driver_id: str) -> bool:
        one_hour_ago = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        one_hour_ago_str = one_hour_ago.strftime(DATE_FORMAT)

        late_records = self.dynamodb.query_table(
            table_name="fo_optimizer_late_drivers",
            key_condition_expression=Key("customer_driver_id").eq(customer_driver_id),
        )

        if not late_records:
            return False

        latest_record = max(late_records, key=lambda x: x["as_of"])
        latest_as_of = datetime.strptime(
            latest_record["as_of"],
            DATE_FORMAT
        ).replace(tzinfo=timezone.utc)

        return latest_as_of >= one_hour_ago

    async def get_customer_entity(self, customer: str) -> Optional[CustomerEntity]:
        return self.customer_service.find(customer)

    async def get_drivers_based_on_customer_or_driver_id(
        self, customer: str, driver_id: str
    ) -> Optional[list[DriverEntity]]:
        if driver_id:
            return [
                self.drivers_service.driver_by_customer_and_id(
                    customer_name=customer, driver_id=driver_id
                )
            ]

        return self.drivers_service.drivers_by_customer(customer)

    async def get_truck_location(
        self, vin: str | None
    ) -> Optional[TruckLocationEntity]:
        if not vin:
            return None

        result = self.dynamodb.query_table(
            table_name=self.dynamodb.TRUCK_LOCATION,
            key_condition_expression=Key("vin").eq(vin),
            scan_index_forward=False,
        )

        if not result:
            self.logger.info(f"Missing data for get_truck_location. Driver VIN: {vin}")
            return None

        latest_item = max(result, key=lambda x: x["as_of"])

        return TruckLocationEntity.model_validate(latest_item)

    async def get_trimble_locations_for_drivers(
        self, drivers: list[DriverEntity]
    ) -> dict[str, tuple[TruckLocationEntity, Address]]:
        location_entitites = await asyncio.gather(
            *[self.get_truck_location(driver.vin) for driver in drivers]
        )

        addresses = self.trimble.coordinates_to_address.batch(
            list(
                map(
                    lambda x: x.coordinates if x else Coordinates(np.nan, np.nan),
                    location_entitites,
                )
            )
        )

        return {
            driver.vin: (location_entity, location)
            for driver, location_entity, location in zip(
                drivers, location_entitites, addresses
            )
        }

    async def get_optimizer_loads(self, customer: str) -> dict[str, ScheduleItemEntity]:
        loads = self.schedule_service.get_schedule_by_customer(customer)

        loads_dict = {}
        for load in loads:
            loads_dict.setdefault(load.driver_id, []).append(load)

        return loads_dict

    def _get_earliest_load(
        self, loads: list[ScheduleItemEntity]
    ) -> Optional[ScheduleItemEntity]:
        if not loads:
            return None

        def key(load: ScheduleItemEntity) -> datetime:
            return self.get_datetime_with_tz(
                load.pickup.date_end, load.pickup.date_end_timezone
            )

        earliest_load = min(
            loads,
            key=key,
        )

        return earliest_load

    def _get_most_recent_load(
        self, loads: list[ScheduleItemEntity]
    ) -> Optional[ScheduleItemEntity]:
        if not loads:
            return None

        def key(load: ScheduleItemEntity) -> datetime:
            return self.get_datetime_with_tz(
                load.dropoff.date_end, load.dropoff.date_end_timezone
            )

        most_recent_load = max(
            loads,
            key=key,
        )

        return most_recent_load

    def select_latest_valid_load(
        self, loads: list[ScheduleItemEntity]
    ) -> Optional[ScheduleItemEntity]:
        """Selects the contract load that is most in the future."""
        if not loads or not isinstance(loads, list):
            return None

        contract_loads = list(filter(lambda x: x.type == LoadTypes.CONTRACT, loads))

        if not contract_loads:
            return None

        if len(contract_loads) == 1:
            return contract_loads[0]

        self.logger.debug(
            f"Returned more than one load. Key: {contract_loads[0].customer_driver_id}"
        )
        return self._get_most_recent_load(contract_loads)

    def _set_cache_result(
        self, current_customer: str, driver_id: str, result: FleetResponseModel
    ) -> None:
        key = (
            STATUS_CACHE_PATTERN_WITH_DRIVER.format(current_customer, driver_id)
            if driver_id
            else STATUS_CACHE_PATTERN.format(current_customer)
        )
        self.cache_service.set(
            key=key,
            value=result.dict(),
            ttl=timedelta(minutes=settings.empty_status_cache_minutes),
        )

    def get_fleet_status_cache(
        self, current_customer: str, driver_id: str
    ) -> Optional[dict]:
        key = (
            STATUS_CACHE_PATTERN_WITH_DRIVER.format(current_customer, driver_id)
            if driver_id
            else STATUS_CACHE_PATTERN.format(current_customer)
        )
        if result := self.cache_service.get(key=key):
            return result
        return None

    async def get_fleet_status(
        self, current_user: CustomerDriver
    ) -> FleetResponseModel:
        current_customer = current_user.customer
        driver_id = current_user.driver_id
        if fleet := self.get_fleet_status_cache(
            current_customer=current_customer, driver_id=driver_id
        ):
            return FleetResponseModel(output=fleet)

        customer, drivers, loads = await asyncio.gather(
            self.get_customer_entity(current_customer),
            self.get_drivers_based_on_customer_or_driver_id(
                customer=current_customer, driver_id=driver_id
            ),
            self.get_optimizer_loads(current_customer),
        )

        truck_locations = await self.get_trimble_locations_for_drivers(drivers)

        driver_status_tasks = []
        for driver in drivers:
            truck_location, truck_address = truck_locations.get(
                driver.vin, (None, None)
            )

            driver_status_tasks.append(
                self.get_driver_status(
                    driver,
                    truck_location,
                    truck_address,
                    loads.get(driver.driver_id),
                    customer,
                )
            )

        driver_status = await asyncio.gather(*driver_status_tasks)

        fleet = FleetModel(fleet_id=current_customer, drivers=driver_status)
        fleet = sort_drivers_by_null_locations(fleet)

        self._set_cache_result(
            current_customer=current_customer, driver_id=driver_id, result=fleet
        )
        return FleetResponseModel(output=fleet)

    async def get_driver_status(
        self,
        driver: DriverEntity,
        truck_location: TruckLocationEntity,
        truck_address: Address,
        loads: list[ScheduleItemEntity],
        customer: Optional[CustomerEntity],
    ) -> DriverStatusModel:
        current_location = self.get_current_location(
            truck_location_address=truck_address, truck_location=truck_location
        )

        next_gap = self._get_current_or_next_gap_load(loads)
        latest_contract_load = self.select_latest_valid_load(loads)

        calculated_expected_empty_time = self.get_driver_empty_time(
            next_gap,
            latest_contract_load,
        )
        calculated_empty_status = self.get_empty_status(
            calculated_expected_empty_time,
            customer_driver_id=f"{driver.customer}_{driver.driver_id}",  # Format customer_driver_id correctly
        )

        home_location = self.get_home_location(
            driver_yard=driver.yard, customer_yard=customer.yard
        )
        required_home_date = self._get_next_home_date(loads)

        empty_location = self.get_empty_location(
            gap=None if calculated_empty_status.is_empty else next_gap,
            current_location=current_location,
        )

        return DriverStatusModel(
            driver=driver,
            empty_status=calculated_empty_status,
            expected_empty_time=calculated_expected_empty_time,
            home_location=home_location if home_location else None,
            current_location=current_location if current_location else None,
            empty_location=empty_location,
            required_home_date=required_home_date,
            updated_at=latest_contract_load.updated_at if latest_contract_load else "",
        )

    def get_driver_empty_time(
        self,
        next_gap: ScheduleItemEntity | None,
        latest_load: ScheduleItemEntity | None,
    ) -> datetime:
        empty_times = []
        now = datetime.now(tz=timezone.utc)

        if not latest_load:
            return now

        latest_load_do = self.get_datetime_with_tz(
            latest_load.dropoff.date_begin, latest_load.dropoff.date_begin_timezone
        )
        if latest_load_do > now:
            empty_times.append(latest_load_do)

        if next_gap:
            empty_times.append(
                self.get_datetime_with_tz(
                    next_gap.pickup.date_begin, next_gap.pickup.date_begin_timezone
                )
            )

        return min(empty_times) if empty_times else now

    def get_datetime_with_tz(self, date_str: str, date_str_timezone: str) -> datetime:
        date_str = self._ensure_correct_dt_format(date_str)
        date_str_timezone = date_str_timezone.removeprefix("UTC")

        if not date_str_timezone or date_str_timezone.lower() == "utc":
            date = datetime.strptime(date_str, DATE_FORMAT)
            return pytz.UTC.localize(date)

        date = datetime.strptime(
            f"{date_str} {date_str_timezone}", DATE_FORMAT_WITH_TIMEZONE
        )

        if not date.tzinfo:
            date = pytz.UTC.localize(date)

        return date

    def _get_current_or_next_gap_load(
        self,
        loads: list[ScheduleItemEntity],
    ) -> Optional[ScheduleItemEntity]:
        """Select the first unfinished gap."""
        if not loads or not isinstance(loads, list):
            return None

        dropoff_after = datetime.now(timezone.utc) + timedelta(hours=12)
        unfinished_loads = list(
            filter(
                lambda x: x.type == LoadTypes.SPOT_LANE
                and self.get_datetime_with_tz(
                    x.dropoff.date_end, x.dropoff.date_end_timezone
                )
                > dropoff_after,
                loads,
            )
        )

        if not unfinished_loads:
            return None

        if len(unfinished_loads) == 1:
            return unfinished_loads[0]

        return self._get_earliest_load(unfinished_loads)

    def _get_next_home_date(
        self, loads: list[ScheduleItemEntity]
    ) -> Optional[datetime]:
        """Select the earliest future home date."""
        if not loads or not isinstance(loads, list):
            return None

        current_date = datetime.now(tz=timezone.utc)
        future_off_duty_loads = list(
            filter(
                lambda x: x.type in {LoadTypes.PTO, LoadTypes.NON_WORKING_DAY}
                and self.get_datetime_with_tz(
                    x.pickup.date_begin, x.pickup.date_begin_timezone
                )
                > current_date,
                loads,
            )
        )

        if not future_off_duty_loads:
            return None

        if len(future_off_duty_loads) == 1:
            load = future_off_duty_loads[0]
            return self.get_datetime_with_tz(
                load.pickup.date_begin, load.pickup.date_begin_timezone
            )

        load = self._get_earliest_load(future_off_duty_loads)
        return self.get_datetime_with_tz(
            load.pickup.date_begin, load.pickup.date_begin_timezone
        )

    @staticmethod
    def _ensure_correct_dt_format(date_end: str) -> str:
        if ":" in date_end:
            return date_end

        parts = date_end.split(" ")
        time = parts[-1].replace("-", ":")
        return f"{parts[0]} {time}"

    def get_empty_status(
        self,
        expected_empty_time: datetime,
        customer_driver_id: str,  # Add customer_driver_id parameter
    ) -> EmptyStatusEnum:
        # Check for LATE status first before other status checks
        if self._check_if_driver_is_late(customer_driver_id):
            return EmptyStatusEnum.LATE

        current_time = datetime.now(tz=timezone.utc)
        time_difference = expected_empty_time - current_time
        if time_difference <= timedelta(minutes=30):
            return EmptyStatusEnum.EMPTY
        elif time_difference < timedelta(hours=24):
            return EmptyStatusEnum.EMPTY_SOON
        elif time_difference < timedelta(hours=48):
            return EmptyStatusEnum.EMPTY_TOMORROW
        else:
            return EmptyStatusEnum.EMPTY_LATER

    @staticmethod
    def get_home_location(
        driver_yard: DriverYard, customer_yard: DriverYard
    ) -> HomeLocationModel:
        use_customer_coordinates = (
            driver_yard.coordinates.lat == 0.0 and driver_yard.coordinates.lon == 0.0
        )

        return HomeLocationModel(
            latitude=(
                customer_yard.coordinates.lat
                if use_customer_coordinates
                else driver_yard.coordinates.lat
            ),
            longitude=(
                customer_yard.coordinates.lon
                if use_customer_coordinates
                else driver_yard.coordinates.lon
            ),
            zip_code=(
                customer_yard.zipcode
                if use_customer_coordinates
                else driver_yard.zipcode
            ),
            country=(
                customer_yard.country
                if use_customer_coordinates
                else driver_yard.country
            ),
            date_begin=datetime.now(tz=timezone.utc),
            address=(
                customer_yard.address
                if use_customer_coordinates
                else driver_yard.address
            ),
            city=customer_yard.city if use_customer_coordinates else driver_yard.city,
            state=(
                customer_yard.state if use_customer_coordinates else driver_yard.state
            ),
        )

    @staticmethod
    def get_empty_location(
        gap: ScheduleItemEntity, current_location: CurrentLocationModel
    ) -> Optional[EmptyLocationModel]:
        if not gap:
            return (
                EmptyLocationModel(**current_location.dict())
                if current_location
                else None
            )

        return EmptyLocationModel(
            latitude=gap.pickup.latitude,
            longitude=gap.pickup.longitude,
            zip_code=gap.pickup.zip_code
            if gap.pickup.zip_code and len(gap.pickup.zip_code) >= 5
            else None,
            country=gap.pickup.country,
            state=gap.pickup.state,
            city=gap.pickup.city,
            address=None,
        )

    @staticmethod
    def get_current_location(
        truck_location_address: Address, truck_location: TruckLocationEntity
    ) -> Optional[CurrentLocationModel]:
        return (
            CurrentLocationModel(
                latitude=truck_location.coordinates.lat,
                longitude=truck_location.coordinates.lon,
                zip_code=truck_location_address.zip or None,
                country=truck_location_address.country,
                state=truck_location_address.state,
                city=truck_location_address.city,
                date_begin=truck_location.as_of,
                address=truck_location_address.street_address,
            )
            if truck_location
            else None
        )
