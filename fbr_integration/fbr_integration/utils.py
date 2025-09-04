import frappe
import io
import json


class FBRRequestError(frappe.ValidationError):
	pass


class FBRConnectionError(FBRRequestError):
	pass


class FBRResponseError(FBRRequestError):
	pass


def get_item_pct_code(item):
	if item.item_code:
		pct_code = frappe.get_cached_value("Item", item.item_code, "customs_tariff_number")
		if pct_code:
			return pct_code
		else:
			return get_item_group_pct_code(frappe.get_cached_value("Item", item.item_code, "item_group"))
	else:
		return None


def get_item_group_pct_code(item_group):
	current_item_group = item_group
	while current_item_group:
		item_group_doc = frappe.get_cached_doc("Item Group", current_item_group)
		if item_group_doc.customs_tariff_number:
			return item_group_doc.customs_tariff_number

		current_item_group = item_group_doc.parent_item_group

	return None


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
	status,
	sales_invoice,
	data,
	invoice_number=None,
	response=None,
	error_type=None,
):
	if isinstance(data, dict):
		data = json.dumps(data)

	error = None
	if status == "Failed":
		error = frappe.get_traceback()

	frappe.enqueue(
		insert_request_log,
		status=status,
		sales_invoice=sales_invoice,
		invoice_data=data,
		fbr_pos_invoice_no=invoice_number,
		response_json=response.text if response else None,
		error_type=error_type,
		error=error
	)


def insert_request_log(
	status,
	sales_invoice,
	data,
	invoice_number=None,
	response_json=None,
	error=None,
	error_type=None,
):
	log_doc = frappe.new_doc("Integration Request")
	log_doc.status = status

	log_doc.reference_doctype = "Sales Invoice"
	log_doc.reference_docname = sales_invoice

	log_doc.data = data
	log_doc.request_id = invoice_number or None
	log_doc.output = response_json
	log_doc.error = error or None
	log_doc.request_description = error_type or None

	log_doc.insert(ignore_permissions=True)
