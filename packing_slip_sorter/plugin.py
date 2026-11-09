import logging

from decimal import Decimal, InvalidOperation

from plugin import InvenTreePlugin
from plugin.mixins import (
  ReportMixin,
  SettingsMixin
)

logger = logging.getLogger('inventree')

class PackingSlipSorterPlugin(ReportMixin, SettingsMixin, InvenTreePlugin):
    NAME = "Packing Slip sorter"
    SLUG = "packingslip-sorter"
    TITLE = "Packing Slip Sorter"
    DESCRIPTION = "Sorteert pakbonregels op de parameter Pakbon volgorde"
    AUTHOR = "Leo Schelvis"
    VERSION = "1.0"

    SETTINGS = {
        "PARAMETER_NAME": {
            "name": "Parameter name",
            "description": "Name of the parameter used on the part to base the sorting on",
            "default": "Packingslip sort order",
            "required": True,
        }
    }

    def add_report_context(self, report_instance, model_instance, request, context):
        if report_instance.model_type != "salesordershipment":
            return

        allocations = context["allocations"]
        if hasattr(allocations, "all"):
            allocations = allocations.all()

        order_by_part = {}

        def sort_key(allocation):
            part = allocation.item.part

            if part.pk not in order_by_part:
                parameter = part.parameters.filter(
                    template__name=self.get_setting('PARAMETER_NAME')
                ).first()

                try:
                    value = Decimal(str(parameter.data)) if parameter else None
                    if value is not None and not value.is_finite():
                        value = None
                except (InvalidOperation, TypeError, ValueError):
                    value = None

                order_by_part[part.pk] = value

            value = order_by_part[part.pk]
            # Onderdelen zonder geldige volgorde komen na onderdelen met een waarde.
            return (
                value is None,
                value if value is not None else Decimal("0"),
                part.name.casefold(),
                allocation.pk,
            )

        context["sorted_allocations"] = sorted(allocations, key=sort_key)