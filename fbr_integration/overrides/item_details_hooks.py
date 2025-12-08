import frappe


def transaction_controller_init_hook(doc):
	if doc.get("force_item_fields"):
		doc.force_item_fields += [
			"fbr_sales_tax_type",
			"fbr_sro_schedule_no",
			"fbr_sro_serial_no",
		]


def get_item_tax_template_details_hook(item_tax_template, args, out):
	sales_tax_type = frappe.get_cached_value("Item Tax Template", item_tax_template, "fbr_sales_tax_type")
	sro_schedule_no = frappe.get_cached_value("Item Tax Template", item_tax_template, "fbr_sro_schedule_no")
	sro_serial_no = frappe.get_cached_value("Item Tax Template", item_tax_template, "fbr_sro_serial_no")
	out["fbr_sales_tax_type"] = sales_tax_type or None
	out["fbr_sro_schedule_no"] = sro_schedule_no or None
	out["fbr_sro_serial_no"] = sro_serial_no or None
