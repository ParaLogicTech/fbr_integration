import frappe
from fbr_integration.fbr_integration.doctype.fbr_digital_invoicing_settings.fbr_digital_invoicing_settings import setup_fbr_di_fields
from frappe.utils.fixtures import sync_fixtures


def execute():
	sync_fixtures("fbr_integration")
	if frappe.db.get_single_value("FBR Digital Invoicing Settings", "enable_fbr_di", cache=False):
		setup_fbr_di_fields()
