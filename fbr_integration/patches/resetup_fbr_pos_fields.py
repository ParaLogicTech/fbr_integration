import frappe
from fbr_integration.fbr_integration.doctype.fbr_pos_settings.fbr_pos_settings import setup_fbr_pos_fields
from frappe.utils.fixtures import sync_fixtures


def execute():
	sync_fixtures("fbr_integration")
	if frappe.db.get_single_value("FBR POS Settings", "enable_fbr_pos", cache=False):
		setup_fbr_pos_fields()
