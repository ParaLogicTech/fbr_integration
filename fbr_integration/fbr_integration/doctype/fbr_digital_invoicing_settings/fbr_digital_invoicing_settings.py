# Copyright (c) 2025, ParaLogic and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint
from frappe.model.document import Document
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from fbr_integration.fbr_integration.utils import remove_fbr_fields


invoice_custom_fields = [
	# At the top
	{"label": "FBR Digital Invoice #", "fieldname": "fbr_di_invoice_no", "fieldtype": "Data",
		"insert_after": "stin", "read_only": 1, "no_copy": 1, "search_index": 1},
	{"label": "Is FBR Digital Invoice", "fieldname": "is_fbr_di_invoice", "fieldtype": "Check",
		"insert_after": "has_stin", "default": 0, "read_only": 1, "no_copy": 1, "in_standard_filter": 1,
		"search_index": 1, "depends_on": "eval:doc.has_stin && !doc.fbr_di_invoice_no"},

	# In FBR Digital Invoicing Transaction Details Tab
	{"label": "FBR DI Invoice Type", "fieldname": "fbr_di_invoice_type", "fieldtype": "Data",
		"insert_after": "fbr_di_details_tab", "read_only": 1, "no_copy": 1},
	{"label": "FBR DI Invoice Ref No", "fieldname": "fbr_di_invoice_ref_no", "fieldtype": "Data",
		"insert_after": "fbr_di_invoice_type", "read_only": 1, "no_copy": 1},
	{"label": "FBR DI Buyer Registration Type", "fieldname": "fbr_di_buyer_registration_type", "fieldtype": "Data",
		"insert_after": "fbr_di_invoice_ref_no", "read_only": 1, "no_copy": 1},

	{"label": "", "fieldname": "cb_fbr_di_1", "fieldtype": "Column Break",
		"insert_after": "fbr_di_buyer_registration_type"},

	{"label": "FBR DI Seller Province", "fieldname": "fbr_di_seller_province", "fieldtype": "Data",
		"insert_after": "cb_fbr_di_1", "read_only": 1, "no_copy": 1},
	{"label": "FBR DI Buyer Province", "fieldname": "fbr_di_buyer_province", "fieldtype": "Data",
		"insert_after": "fbr_di_seller_province", "read_only": 1, "no_copy": 1},

	# Hidden Fields
	{"label": "FBR DI QR Code", "fieldname": "fbr_di_qrcode", "fieldtype": "Barcode",
		"insert_after": "fbr_di_buyer_province", "read_only": 1, "hidden": 1, "no_copy": 1},
	{"label": "FBR DI JSON Data", "fieldname": "fbr_di_json_data", "fieldtype": "Code",
		"insert_after": "fbr_di_qrcode", "read_only": 1, "hidden": 1, "no_copy": 1},

	# FBR Digital Invoicing Item Details Section
	{"label": "FBR Digital Invoice Items", "fieldname": "sec_fbr_di_item_details", "fieldtype": "Section Break",
		"insert_after": "fbr_di_json_data", "collapsible": 0,
		"depends_on": "eval:doc.is_fbr_di_invoice"},

	{"label": "FBR Digital Invoice Items", "fieldname": "fbr_di_items", "fieldtype": "Table",
		"options": "FBR DI Invoice Item",
		"insert_after": "sec_fbr_di_item_details", "read_only": 1, "no_copy": 1},
]

custom_fields_map = {
	'Sales Invoice': invoice_custom_fields,
}

for d in invoice_custom_fields:
	d['translatable'] = 0


class FBRDigitalInvoicingSettings(Document):
	def validate(self):
		if self.enable_fbr_di:
			self.validate_fbr_di_enabled_from_site_config()
			setup_fbr_di_fields()
		else:
			disable_fbr_di()

	def validate_fbr_di_enabled_from_site_config(self):
		if not cint(frappe.conf.get('enable_fbr_di')):
			frappe.throw(_("FBR Digital Invoicing is not enabled from the backend. Please contact your system administrator."))


def setup_fbr_di_fields():
	create_custom_fields(custom_fields_map)


def disable_fbr_di():
	meta = frappe.get_meta("Sales Invoice")
	if meta.has_field('is_fbr_di_invoice'):
		if can_remove_fbr_di_fields():
			remove_fbr_fields(custom_fields_map)


def can_remove_fbr_di_fields():
	if frappe.db.get_all("Sales Invoice", {'docstatus': 1, 'is_fbr_di_invoice': 1}, limit=1):
		return False
	else:
		return True
