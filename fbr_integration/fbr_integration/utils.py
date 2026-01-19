import frappe
from frappe import _
from frappe.utils import cint
from erpnext.stock.get_item_details import get_customs_tariff_number
import io
import json


class FBRRequestError(frappe.ValidationError):
	pass


class FBRConnectionError(FBRRequestError):
	pass


class FBRResponseError(FBRRequestError):
	pass


def get_item_hs_code(row, invoice):
	parent_dict = invoice.get_item_details_parent_args()
	args = invoice.get_item_details_child_args(row, parent_dict)
	item_doc = frappe.get_cached_doc("Item", row.item_code) if row.item_code else frappe._dict()
	return get_customs_tariff_number(item_doc, args) or ""


def get_item_tax_details(row, invoice, account):
	if not account:
		return frappe._dict()

	taxes = invoice.get_taxes_for_item(row)
	tax_row = [d for d in taxes if d.account_head == account]

	if not tax_row:
		return frappe._dict()
	elif len(tax_row) > 1:
		frappe.throw(_("Row #{0}: Tax Account {1} is duplicated").format(tax_row[-1].idx, account))

	return tax_row[0]


def get_invoice_qrcode_svg(invoice_number):
	from pyqrcode import create as qrcreate
	qrcode = qrcreate(invoice_number)
	svg = ''
	stream = io.BytesIO()
	try:
		qrcode.svg(stream, scale=2, background="#fff", module_color="#000", quiet_zone=1, omithw=True)
		svg = stream.getvalue().decode().replace('\n', '')
	finally:
		stream.close()

	return svg


def log_fbr_request(
	service,
	status,
	sales_invoice,
	data,
	invoice_number=None,
	response=None,
	error_type=None,
	existing_log=None,
	auto_commit=False,
	enqueue=False,
):
	if isinstance(data, dict):
		data = json.dumps(data)

	error = None
	if status == "Failed":
		error = frappe.get_traceback()

	return frappe.enqueue(
		insert_request_log,
		service=service,
		status=status,
		sales_invoice=sales_invoice,
		data=data,
		invoice_number=invoice_number,
		response_json=response.text if response else None,
		error_type=error_type,
		error=error,
		existing_log=existing_log,
		auto_commit=auto_commit,
		now=not enqueue,
	)


def insert_request_log(
	service,
	status,
	sales_invoice,
	data,
	invoice_number=None,
	response_json=None,
	error=None,
	error_type=None,
	existing_log=None,
	auto_commit=False,
):
	if existing_log:
		log_doc = existing_log
	else:
		log_doc = frappe.new_doc("Integration Request")

	log_doc.integration_request_service = service
	log_doc.status = status

	log_doc.reference_doctype = "Sales Invoice"
	log_doc.reference_docname = sales_invoice

	log_doc.data = data
	log_doc.request_id = invoice_number or None
	log_doc.output = response_json
	log_doc.error = error or None
	log_doc.request_description = error_type or None

	log_doc.save(ignore_permissions=True)

	if auto_commit:
		frappe.db.commit()

	return log_doc


def remove_fbr_fields(custom_fields_map):
	for dt, custom_fields in custom_fields_map.items():
		for custom_field_detail in custom_fields:
			custom_field_name = frappe.db.get_value('Custom Field', {
				"dt": dt, "fieldname": custom_field_detail.get('fieldname')
			})
			if custom_field_name:
				frappe.delete_doc('Custom Field', custom_field_name, delete_permanently=True)


def override_sales_invoice_dashboard(data):
	data.setdefault("non_standard_fieldnames", {})
	data["non_standard_fieldnames"]["Integration Request"] = "reference_docname"

	enable_fbr_pos = cint(frappe.get_cached_value("FBR POS Settings", None, "enable_fbr_pos"))
	enable_fbr_di = cint(frappe.get_cached_value("FBR Digital Invoicing Settings", None, "enable_fbr_di"))
	if not enable_fbr_di and not enable_fbr_pos:
		return data

	items = ["Integration Request"]

	reference_sec = [d for d in data["transactions"] if d.get("label") == _("Reference")]
	reference_sec = reference_sec[0] if reference_sec else None
	if reference_sec:
		reference_sec["items"] += items
	else:
		data["transactions"].append({
			"label": _("Reference"),
			"items": items
		})

	return data
