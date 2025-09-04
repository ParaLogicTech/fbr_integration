import frappe
import click


def execute():
	fields_map = {
		"owner": "owner",
		"creation": "creation",
		"modified": "modified",
		"modified_by": "modified_by",
		"fbr_pos_invoice_no": "request_id",
		"error_type": "request_description",
		"invoice_data": "data",
		"response": "output",
		"error": "error",
	}

	url = frappe.db.get_single_value("FBR POS Settings", "post_invoice_data_endpoint")

	logs = frappe.db.sql("select * from `tabFBR POS Log` order by creation", as_dict=True)
	with click.progressbar(logs) as data:
		for log in data:
			doc = frappe.new_doc("Integration Request")

			for old_f, new_f in fields_map.items():
				doc.set(new_f, log.get(old_f))

			doc.integration_request_service = "FBR POS"
			doc.url = url

			doc.status = "Failed" if log.log_type == "Error" else "Completed"

			if log.sales_invoice:
				doc.reference_doctype = "Sales Invoice"
				doc.reference_docname = log.sales_invoice

			doc.insert(ignore_links=True)
