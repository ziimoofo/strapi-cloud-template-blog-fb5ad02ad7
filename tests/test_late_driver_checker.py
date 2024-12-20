import pytest
from datetime import datetime, timezone, timedelta
from dateutil import tz
from unittest.mock import Mock, patch

from late_driver_checker.services.late_driver_checker_service import LateDriverCheckerService
from shared.models.enums.load_types import LoadTypes

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
def service(logger, customer_service, drivers_service, dynamodb):
    return LateDriverCheckerService(
        logger=logger,
        customer_service=customer_service,
        drivers_service=drivers_service,
        dynamodb=dynamodb
    )

class TestLateDriverCheckerService:
    def test_check_driver_lateness_with_active_load(self, service, logger, dynamodb):
        driver = Mock(customer='roadstar', driver_id='BRID', vin='123')
        customer = Mock(default_timezone='America/New_York', name='Roadstar')
        empty_status_record = {'empty': False}

        # Create a schedule with an active load
        now = datetime.now(tz=timezone.utc)
        ny_tz = tz.gettz('America/New_York')
        schedule_records = [{
            'type': LoadTypes.CONTRACT,
            'pickup': {
                'date_begin': (now.astimezone(ny_tz) - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
            },
            'dropoff': {
                'date_end': (now.astimezone(ny_tz) + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
            }
        }]

        with patch.object(service.dynamodb, 'query_table', return_value=schedule_records):
            service._check_driver_lateness(driver, empty_status_record, customer)
            logger.info.assert_not_called()
            dynamodb.save_item.assert_not_called()

    def test_check_driver_lateness_with_recent_load_and_not_empty(self, service, logger, dynamodb):
        driver = Mock(customer='roadstar', driver_id='BRID', vin='123')
        customer = Mock(default_timezone='America/New_York', name='Roadstar')
        empty_status_record = {'empty': False}

        # Create a schedule with a recently completed load
        now = datetime.now(tz=timezone.utc)
        ny_tz = tz.gettz('America/New_York')
        schedule_records = [
            {
                'type': LoadTypes.CONTRACT,
                'pickup': {
                    'date_begin': (now.astimezone(ny_tz) - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M:%S')
                },
                'dropoff': {
                    'date_end': (now.astimezone(ny_tz) - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
                }
            },
            {
                'type': LoadTypes.NON_WORKING_DAY,  # This should be filtered out
                'pickup': {'date_begin': '2024-01-01 00:00:00'},
                'dropoff': {'date_end': '2024-01-01 23:59:59'}
            }
        ]

        with patch.object(service.dynamodb, 'query_table', return_value=schedule_records):
            service._check_driver_lateness(driver, empty_status_record, customer)
            logger.info.assert_called_once()
            dynamodb.save_item.assert_called_once_with(
                table_name='fo_optimizer_late_drivers',
                item={
                    'customer_driver_id': 'roadstar_BRID',
                    'customer': customer.name,
                    'driver_id': 'BRID',
                    'as_of': pytest.approx(now.strftime('%Y-%m-%d %H:%M:%S'), abs=timedelta(minutes=1))
                }
            )

    def test_check_driver_lateness_with_recent_load_but_empty(self, service, logger, dynamodb):
        driver = Mock(customer='roadstar', driver_id='BRID', vin='123')
        customer = Mock(default_timezone='America/New_York', name='Roadstar')
        empty_status_record = {'empty': True}  # Truck is empty

        # Create a schedule with a recently completed load
        now = datetime.now(tz=timezone.utc)
        ny_tz = tz.gettz('America/New_York')
        schedule_records = [{
            'type': LoadTypes.CONTRACT,
            'pickup': {
                'date_begin': (now.astimezone(ny_tz) - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M:%S')
            },
            'dropoff': {
                'date_end': (now.astimezone(ny_tz) - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
            }
        }]

        with patch.object(service.dynamodb, 'query_table', return_value=schedule_records):
            service._check_driver_lateness(driver, empty_status_record, customer)
            logger.info.assert_not_called()
            dynamodb.save_item.assert_not_called()

    def test_check_driver_lateness_with_old_load(self, service, logger, dynamodb):
        driver = Mock(customer='roadstar', driver_id='BRID', vin='123')
        customer = Mock(default_timezone='America/New_York', name='Roadstar')
        empty_status_record = {'empty': False}

        # Create a schedule with an old completed load
        now = datetime.now(tz=timezone.utc)
        ny_tz = tz.gettz('America/New_York')
        schedule_records = [{
            'type': LoadTypes.CONTRACT,
            'pickup': {
                'date_begin': (now.astimezone(ny_tz) - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
            },
            'dropoff': {
                'date_end': (now.astimezone(ny_tz) - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
            }
        }]

        with patch.object(service.dynamodb, 'query_table', return_value=schedule_records):
            service._check_driver_lateness(driver, empty_status_record, customer)
            logger.info.assert_not_called()
            dynamodb.save_item.assert_not_called()

    def test_check_driver_lateness_with_empty_schedule(self, service, logger, dynamodb):
        driver = Mock(customer='roadstar', driver_id='BRID', vin='123')
        customer = Mock(default_timezone='America/New_York', name='Roadstar')
        empty_status_record = {'empty': False}

        with patch.object(service.dynamodb, 'query_table', return_value=[]):
            service._check_driver_lateness(driver, empty_status_record, customer)
            logger.info.assert_not_called()
            dynamodb.save_item.assert_not_called()

    def test_check_driver_lateness_exactly_24h_ago(self, service, logger, dynamodb):
        driver = Mock(customer='roadstar', driver_id='BRID', vin='123')
        customer = Mock(default_timezone='America/New_York', name='Roadstar')
        empty_status_record = {'empty': False}

        # Create a schedule with a load that ended exactly 24 hours ago
        now = datetime.now(tz=timezone.utc)
        ny_tz = tz.gettz('America/New_York')
        schedule_records = [{
            'type': LoadTypes.CONTRACT,
            'pickup': {
                'date_begin': (now.astimezone(ny_tz) - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
            },
            'dropoff': {
                'date_end': (now.astimezone(ny_tz) - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
            }
        }]

        with patch.object(service.dynamodb, 'query_table', return_value=schedule_records):
            service._check_driver_lateness(driver, empty_status_record, customer)
            logger.info.assert_not_called()
            dynamodb.save_item.assert_not_called()

    def test_check_driver_lateness_with_invalid_date_format(self, service, logger, dynamodb):
        driver = Mock(customer='roadstar', driver_id='BRID', vin='123')
        customer = Mock(default_timezone='America/New_York', name='Roadstar')
        empty_status_record = {'empty': False}

        schedule_records = [{
            'type': LoadTypes.CONTRACT,
            'pickup': {
                'date_begin': 'invalid-date-format'
            },
            'dropoff': {
                'date_end': 'invalid-date-format'
            }
        }]

        with patch.object(service.dynamodb, 'query_table', return_value=schedule_records):
            with pytest.raises(ValueError):
                service._check_driver_lateness(driver, empty_status_record, customer)
            logger.info.assert_not_called()
            dynamodb.save_item.assert_not_called()
