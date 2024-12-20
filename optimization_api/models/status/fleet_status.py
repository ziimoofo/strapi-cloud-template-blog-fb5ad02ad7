from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator

from shared.models.api.date_place_input import DatePlaceInputModel
from shared.persistence.models.entities.driver_entity import DriverEntity

# NOTE: Temporary solution to order by country
MEXICO_COUNTRY = "mexico"
CANADA_COUNTRY = "canada"


class EmptyStatusEnum(str, Enum):
    EMPTY = "EMPTY"
    EMPTY_SOON = "EMPTY_SOON"
    EMPTY_TOMORROW = "EMPTY_TOMORROW"
    EMPTY_LATER = "EMPTY_LATER"
    LATE = "LATE"  # Added new status for late drivers

    @property
    def is_empty(self) -> bool:
        # LATE status is considered not empty since the driver is actively late
        return self == EmptyStatusEnum.EMPTY


class HomeLocationModel(DatePlaceInputModel):
    address: Optional[str] = Field(default=None)

    @model_validator(mode="after")
    def date_end_must_not_be_before_date_begin(self):
        return self


class CurrentLocationModel(DatePlaceInputModel):
    pass


class EmptyLocationModel(DatePlaceInputModel):
    date_begin: Optional[datetime] = Field(default=None)


class DriverStatusModel(BaseModel):
    driver: DriverEntity
    empty_status: EmptyStatusEnum = EmptyStatusEnum.EMPTY_LATER
    expected_empty_time: datetime
    home_location: Optional[HomeLocationModel]
    current_location: Optional[CurrentLocationModel]
    empty_location: Optional[EmptyLocationModel]
    required_home_date: Optional[datetime]
    updated_at: Optional[str] = Field(default=None)


class FleetModel(BaseModel):
    fleet_id: str = Field(..., example="Fleet123", description="The name of the fleet.")
    drivers: List[DriverStatusModel] = Field(
        ..., description="A list of drivers with their details."
    )


class FleetResponseModel(BaseModel):
    output: FleetModel = Field(
        ..., description="The empty status for each driver in the fleet."
    )


def sort_drivers_by_null_locations(fleet: FleetModel) -> FleetModel:
    sorted_drivers = sorted(
        fleet.drivers,
        key=lambda driver: (
            driver.current_location.country.lower() == MEXICO_COUNTRY
            if driver.current_location and driver.current_location.country
            else False,
            driver.current_location.country.lower() == CANADA_COUNTRY
            if driver.current_location and driver.current_location.country
            else False,
            driver.empty_location.country.lower() == MEXICO_COUNTRY
            if driver.empty_location and driver.empty_location.country
            else False,
            driver.empty_location.country.lower() == CANADA_COUNTRY
            if driver.empty_location and driver.empty_location.country
            else False,
            driver.home_location is None,
            driver.current_location is None,
            driver.empty_location is None,
        ),
    )
    fleet.drivers = sorted_drivers
    return fleet
