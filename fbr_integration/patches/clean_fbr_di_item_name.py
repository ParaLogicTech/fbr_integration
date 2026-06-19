import frappe
from fbr_integration.fbr_integration.fbr_di_integration import clean_string


def execute():
	if not frappe.get_meta("Sales Invoice").has_field("is_fbr_di_invoice"):
		return

	invoices = frappe.db.sql_list("""
		select distinct si.name
		from `tabFBR DI Invoice Item` di
		inner join `tabSales Invoice` si on si.name = di.parent
		where si.is_fbr_di_invoice = 1
			and (si.fbr_di_invoice_no = '' or si.fbr_di_invoice_no is null)
			and (di.fbr_di_item_name like '%%\n%%' or di.fbr_di_item_name like '%%\r%%')
	""")

	if invoices:
		print("Cleaning FBR DI Item Description:")

	for name in invoices:
		doc = frappe.get_doc("Sales Invoice", name)
		if doc.docstatus == 1:
			print(name)

		for d in doc.fbr_di_items:
			before = d.fbr_di_item_name
			d.fbr_di_item_name = clean_string(d.fbr_di_item_name)

			if d.fbr_di_item_name != before:
				d.db_set("fbr_di_item_name", d.fbr_di_item_name, update_modified=False)
