import frappe
from frappe.utils.fixtures import sync_fixtures


def execute():
	sync_fixtures("fbr_integration")

	hs_codes = frappe.get_all("Customs Tariff Number", pluck="name")
	for name in hs_codes:
		doc = frappe.get_doc("Customs Tariff Number", name)
		if doc.get("fbr_convert_uom"):
			doc.fbr_qty_uom = "Convert to UOM"
		elif doc.get("fbr_use_alt_uom"):
			doc.fbr_qty_uom = "Contents UOM Qty"
		else:
			doc.fbr_qty_uom = "Transaction UOM Qty"

		doc.db_set("fbr_qty_uom", doc.fbr_qty_uom, update_modified=False)

	frappe.delete_doc_if_exists("Custom Field", "Customs Tariff Number-fbr_use_alt_uom")
