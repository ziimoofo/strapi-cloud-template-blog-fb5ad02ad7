import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch

from boto3.dynamodb.conditions import Key
from optimization_api.models.status.fleet_status import EmptyStatusEnum
from optimization_api.services.empty_status_service import EmptyStatusService


@pytest.fixture
def logger():
    return Mock()

@pytest.fixture
def customer_service():
    return Mock()

@pytest.fixture
def drivers_service():
    return Mock()

@pytest.fixture
def dynamodb():
    return Mock()

@pytest.fixture
def trimble():
    return Mock()

@pytest.fixture
def schedule_service():
    return Mock()

@pytest.fixture
def cache_service():
    return Mock()

@pytest.fixture
def service(logger, customer_service, drivers_service, dynamodb, trimble, schedule_service, cache_service):
    return EmptyStatusService(
        logger=logger,
        dynamodb=dynamodb,
        trimble=trimble,
        schedule_service=schedule_service,
        customer_service=customer_service,
        drivers_service=drivers_service,
        cache_service=cache_service
    )

class TestEmptyStatusService:
    def test_get_empty_status_with_recent_late_record(self, service, dynamodb):
        # Arrange
        now = datetime.now(tz=timezone.utc)
        expected_empty_time = now + timedelta(hours=2)
        customer_driver_id = "roadstar_BRID"

        late_record = {
            'customer_driver_id': customer_driver_id,
            'as_of': (now - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S'),
            'customer': 'Roadstar',
            'driver_id': 'BRID'
        }

        # Mock DynamoDB query to return the late record
        dynamodb.query_table.return_value = [late_record]

        # Act
        status = service.get_empty_status(expected_empty_time, customer_driver_id)

        # Assert
        assert status == EmptyStatusEnum.LATE
        dynamodb.query_table.assert_called_once_with(
            table_name="fo_optimizer_late_drivers",
            key_condition_expression=Key("customer_driver_id").eq(customer_driver_id)
        )

    def test_get_empty_status_with_old_late_record(self, service, dynamodb):
        # Arrange
        now = datetime.now(tz=timezone.utc)
        expected_empty_time = now + timedelta(hours=2)
        customer_driver_id = "roadstar_BRID"

        late_record = {
            'customer_driver_id': customer_driver_id,
            'as_of': (now - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S'),
            'customer': 'Roadstar',
            'driver_id': 'BRID'
        }

        # Mock DynamoDB query to return the old late record
        dynamodb.query_table.return_value = [late_record]

        # Act
        status = service.get_empty_status(expected_empty_time, customer_driver_id)

        # Assert
        assert status == EmptyStatusEnum.EMPTY_TOMORROW
        dynamodb.query_table.assert_called_once_with(
            table_name="fo_optimizer_late_drivers",
            key_condition_expression=Key("customer_driver_id").eq(customer_driver_id)
        )

    def test_get_empty_status_with_no_late_record(self, service, dynamodb):
        # Arrange
        now = datetime.now(tz=timezone.utc)
        expected_empty_time = now + timedelta(minutes=15)
        customer_driver_id = "roadstar_BRID"

        # Mock DynamoDB query to return no late records
        dynamodb.query_table.return_value = []

        # Act
        status = service.get_empty_status(expected_empty_time, customer_driver_id)

        # Assert
        assert status == EmptyStatusEnum.EMPTY
        dynamodb.query_table.assert_called_once_with(
            table_name="fo_optimizer_late_drivers",
            key_condition_expression=Key("customer_driver_id").eq(customer_driver_id)
        )

    def test_get_empty_status_with_multiple_late_records(self, service, dynamodb):
        # Arrange
        now = datetime.now(tz=timezone.utc)
        expected_empty_time = now + timedelta(hours=2)
        customer_driver_id = "roadstar_BRID"

        late_records = [
            {
                'customer_driver_id': customer_driver_id,
                'as_of': (now - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S'),
                'customer': 'Roadstar',
                'driver_id': 'BRID'
            },
            {
                'customer_driver_id': customer_driver_id,
                'as_of': (now - timedelta(minutes=45)).strftime('%Y-%m-%d %H:%M:%S'),
                'customer': 'Roadstar',
                'driver_id': 'BRID'
            }
        ]

        # Mock DynamoDB query to return multiple late records
        dynamodb.query_table.return_value = late_records

        # Act
        status = service.get_empty_status(expected_empty_time, customer_driver_id)

        # Assert
        assert status == EmptyStatusEnum.LATE
        dynamodb.query_table.assert_called_once_with(
            table_name="fo_optimizer_late_drivers",
            key_condition_expression=Key("customer_driver_id").eq(customer_driver_id)
        )

    def test_get_empty_status_empty_soon(self, service, dynamodb):
        # Arrange
        now = datetime.now(tz=timezone.utc)
        expected_empty_time = now + timedelta(hours=12)
        customer_driver_id = "roadstar_BRID"

        # Mock DynamoDB query to return no late records
        dynamodb.query_table.return_value = []

        # Act
        status = service.get_empty_status(expected_empty_time, customer_driver_id)

        # Assert
        assert status == EmptyStatusEnum.EMPTY_SOON
        dynamodb.query_table.assert_called_once_with(
            table_name="fo_optimizer_late_drivers",
            key_condition_expression=Key("customer_driver_id").eq(customer_driver_id)
        )

    def test_get_empty_status_empty_later(self, service, dynamodb):
        # Arrange
        now = datetime.now(tz=timezone.utc)
        expected_empty_time = now + timedelta(days=3)
        customer_driver_id = "roadstar_BRID"

        # Mock DynamoDB query to return no late records
        dynamodb.query_table.return_value = []

        # Act
        status = service.get_empty_status(expected_empty_time, customer_driver_id)

        # Assert
        assert status == EmptyStatusEnum.EMPTY_LATER
        dynamodb.query_table.assert_called_once_with(
            table_name="fo_optimizer_late_drivers",
            key_condition_expression=Key("customer_driver_id").eq(customer_driver_id)
        )
