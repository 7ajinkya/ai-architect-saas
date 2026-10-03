from ai_architect.domain.capacity.models import (
    CapacityModelInput,
    CapacityModelResult,
)


SECONDS_PER_DAY = 86_400

FORMULA_VERSION = "v1"


class CapacityCalculator:
    def calculate(
        self,
        inputs: CapacityModelInput,
    ) -> CapacityModelResult:
        daily_messages = (
            inputs.daily_active_users
            * inputs.messages_per_user_per_day
        )

        average_write_qps = (
            daily_messages
            / SECONDS_PER_DAY
        )

        peak_write_qps = (
            average_write_qps
            * inputs.peak_factor
        )

        peak_read_qps = (
            peak_write_qps
            * inputs.read_write_ratio
        )

        concurrent_connections = (
            inputs.daily_active_users
            * inputs.connection_factor
        )

        storage_growth_bytes_per_day = (
            daily_messages
            * inputs.average_message_size_bytes
        )

        hot_working_set_bytes = (
            storage_growth_bytes_per_day
            * inputs.storage_retention_days
        )

        egress_bytes_per_second = (
            peak_read_qps
            * inputs.average_message_size_bytes
        )

        return CapacityModelResult(
            peak_write_qps=peak_write_qps,
            peak_read_qps=peak_read_qps,
            concurrent_connections=concurrent_connections,
            storage_growth_bytes_per_day=storage_growth_bytes_per_day,
            hot_working_set_bytes=hot_working_set_bytes,
            egress_bytes_per_second=egress_bytes_per_second,
            formula_version=FORMULA_VERSION,
            calculation_trace={
                "peak_write_qps": (
                    "daily_active_users × "
                    "messages_per_user_per_day ÷ "
                    "86400 × peak_factor"
                ),
                "peak_read_qps": (
                    "peak_write_qps × read_write_ratio"
                ),
                "concurrent_connections": (
                    "daily_active_users × connection_factor"
                ),
                "storage_growth_bytes_per_day": (
                    "daily_active_users × "
                    "messages_per_user_per_day × "
                    "average_message_size_bytes"
                ),
                "hot_working_set_bytes": (
                    "storage_growth_bytes_per_day × "
                    "storage_retention_days"
                ),
                "egress_bytes_per_second": (
                    "peak_read_qps × "
                    "average_message_size_bytes"
                ),
            },
        )