import frappe
from frappe import _
from frappe.utils import cint, flt, cstr, getdate, strip_html, clean_whitespace
from fbr_integration.fbr_integration.utils import (
	get_invoice_qrcode_svg,
	log_fbr_request,
	get_item_hs_code,
	get_item_tax_details,
	FBRRequestError,
	FBRResponseError,
	FBRConnectionError,
)
import json
import requests


DEFAULT_UOM = "Numbers, pieces, units"


def validate_fbr_di_invoice(invoice, method=None):
	if not invoice.meta.has_field('is_fbr_di_invoice'):
		return

	# For manual editing
	if invoice.get("fbr_di_invoice_no") and invoice.amended_from:
		return

	# Reset values for draft
	if invoice.docstatus == 0:
		reset_values_for_draft_invoice(invoice)

	# Determine if FBR Digital Invoice
	invoice.is_fbr_di_invoice = determine_is_fbr_di(invoice)

	# Calculate FBR DI Invoice and Item Values
	if cint(invoice.get('is_fbr_di_invoice')):
		calculate_fbr_di_values(invoice)

	# If no FBR DI Items, not an FBR Digtal Invoice
	if not invoice.get('fbr_di_items'):
		invoice.is_fbr_di_invoice = 0
	# If no taxes charged or no taxable
	if not any([d.fbr_di_sale_value or d.fbr_di_sales_tax for d in invoice.get('fbr_di_items')]):
		invoice.is_fbr_di_invoice = 0

	if cint(invoice.get('is_fbr_di_invoice')):
		# Validate FBR DI
		validate_is_fbr_di(invoice)
	else:
		# Not an FBR DI Reset Values
		reset_values_for_not_fbr_di(invoice)


def before_cancel_fbr_di_invoice(invoice, method=None):
	if not invoice.meta.has_field('is_fbr_di_invoice'):
		return

	if cint(invoice.get('is_fbr_di_invoice')) and invoice.get('fbr_di_invoice_no'):
		# Allow cancelling ONLY if explicity allowed to cancel and and in developer mode
		if (
			invoice.flags.allow_fbr_di_cancellation
			or (frappe.conf.get('allow_fbr_di_cancellation') and frappe.conf.get("developer_mode"))
		):
			pass
		else:
			frappe.throw(_("Cannot cancel FBR Digital Invoice because it is already posted. Please make a Credit Note instead."))


def on_submit_fbr_di_invoice(invoice, method=None):
	if not check_fbr_di_enabled():
		return

	# For manual editing
	if invoice.get("fbr_di_invoice_no") and invoice.amended_from:
		return

	ignore_connection_error = cint(frappe.get_cached_value("FBR Digital Invoicing Settings", None, "ignore_connection_error_on_submit"))
	post_fbr_di_invoice(invoice, ignore_connection_error=ignore_connection_error, auto_commit=True)
	if frappe.flags.fbr_di_connection_error:
		frappe.msgprint(_(
			"FBR Digital Invoice Number could not be generated because of a connection error to the FBR Digital Invoicing Service.<br><br>"
			"System will attempt to generate FBR Digital Invoice Number again in the background. "
			"You can also retry manually by clicking the 'Sync FBR Digital Invoice' button."
		), title=_("FBR Digital Invoicing Service Connection Failed"))


def determine_is_fbr_di(invoice):
	enable_fbr_di = cint(frappe.get_cached_value("FBR Digital Invoicing Settings", None, "enable_fbr_di"))

	if enable_fbr_di and cint(invoice.get('has_stin')):
		return 1
	else:
		return 0


def check_fbr_di_enabled(throw=False):
	if not cint(frappe.conf.get('enable_fbr_di')):
		if throw:
			frappe.throw(_("FBR Digital Invoicing is not enabled from the backend. Please contact your system administrator."))
		return False

	if not cint(frappe.db.get_single_value("FBR Digital Invoicing Settings", "enable_fbr_di")):
		if throw:
			frappe.throw(_("FBR Digital Invoicing is not enabled from FBR Digital Invoicing Settings. Please contact your system administrator."))
		return False

	return True


def validate_is_fbr_di(invoice):
	pass


def reset_values_for_draft_invoice(invoice):
	invoice.fbr_di_invoice_no = None
	invoice.fbr_di_qrcode = None
	invoice.fbr_di_json_data = None


def reset_values_for_not_fbr_di(invoice):
	invoice.fbr_di_invoice_no = None
	invoice.fbr_di_qrcode = None
	invoice.fbr_di_json_data = None
	invoice.fbr_di_invoice_type = None
	invoice.fbr_di_invoice_ref_no = None
	invoice.fbr_di_buyer_registration_type = None
	invoice.fbr_di_seller_province = None
	invoice.fbr_di_buyer_province = None
	invoice.fbr_di_items = []


def calculate_fbr_di_values(invoice):
	sales_tax_account = frappe.get_cached_value('Company', invoice.company, "sales_tax_account")
	extra_tax_account = frappe.get_cached_value('Company', invoice.company, "extra_tax_account")
	further_tax_account = frappe.get_cached_value('Company', invoice.company, "further_tax_account")

	if not sales_tax_account:
		frappe.throw(_("Please set Sales Tax Account in {0} to calculate FBR Digital Invoice Data")
			.format(frappe.get_desk_link("Company", invoice.company)))

	invoice.fbr_di_invoice_type, invoice.fbr_di_invoice_ref_no = get_invoice_type_and_ref(invoice)

	invoice.fbr_di_buyer_registration_type = "Registered" if invoice.tax_strn else "Unregistered"

	invoice.fbr_di_seller_province = get_province_from_address(invoice.customer_address)
	invoice.fbr_di_buyer_province = get_province_from_address(invoice.company_address)

	# Create Item Row ID Map
	item_map = {}
	existing_di_item_map = {}
	for d in invoice.items:
		item_map[d.name] = d
	for d in invoice.get('fbr_di_items'):
		if d.fbr_di_item_reference:
			existing_di_item_map[d.fbr_di_item_reference] = d

	# Items
	invoice.fbr_di_items = []
	for item in invoice.items:
		existing_di_item = existing_di_item_map.get(item.name)
		di_item = invoice.append('fbr_di_items', existing_di_item)

		# Reference to line item
		di_item.fbr_di_item_reference = item.name

		# Item Code / Type
		di_item.fbr_di_item_name = item.item_name
		di_item.fbr_di_hs_code = get_item_hs_code(item)
		di_item.fbr_di_sale_type = get_item_sale_type(item)

		# Qty / Amounts
		qty, uom = get_item_qty_and_uom(item)
		di_item.fbr_di_quantity = flt(qty, di_item.precision('fbr_di_quantity'))
		di_item.fbr_di_uom = uom
		di_item.fbr_di_sale_value = flt(item.base_net_amount, di_item.precision('fbr_di_sale_value'))
		di_item.fbr_di_retail_value = flt(item.base_taxable_amount, di_item.precision('fbr_di_retail_value')) if cint(item.apply_taxes_on_retail) else 0
		di_item.fbr_di_discount = flt(item.base_tax_exclusive_total_discount, di_item.precision('fbr_di_discount'))

		# Taxes
		sales_tax_details = get_item_tax_details(item, invoice, sales_tax_account)
		extra_tax_details = get_item_tax_details(item, invoice, extra_tax_account)
		further_tax_details = get_item_tax_details(item, invoice, further_tax_account)

		di_item.fbr_di_tax_rate = flt(sales_tax_details.rate,
			di_item.precision('fbr_di_tax_rate'))
		di_item.fbr_di_sales_tax = flt(sales_tax_details.tax_amount_after_discount_amount,
			di_item.precision('fbr_di_sales_tax'))
		di_item.fbr_di_extra_tax = flt(extra_tax_details.tax_amount_after_discount_amount,
			di_item.precision('fbr_di_extra_tax'))
		di_item.fbr_di_further_tax = flt(further_tax_details.tax_amount_after_discount_amount,
			di_item.precision('fbr_di_further_tax'))

		di_item.fbr_di_sales_tax_withheld = 0
		di_item.fbr_di_fed_tax = 0

		di_item.fbr_di_total_value = flt(
			di_item.fbr_di_sale_value + di_item.fbr_di_sales_tax + di_item.fbr_di_further_tax,
			di_item.precision('fbr_di_total_value')
		)

		# reverse sign for credit note
		if invoice.is_return:
			di_item.fbr_di_quantity *= -1
			di_item.fbr_di_sale_value *= -1
			di_item.fbr_di_retail_value *= -1
			di_item.fbr_di_discount *= -1
			di_item.fbr_di_sales_tax *= -1
			di_item.fbr_di_extra_tax *= -1
			di_item.fbr_di_further_tax *= -1
			di_item.fbr_di_sales_tax_withheld *= -1
			di_item.fbr_di_fed_tax *= -1
			di_item.fbr_di_total_value *= -1

		# Additional
		di_item.fbr_di_sro_schedule_no = ""
		di_item.fbr_di_sro_serial_no = ""

		# Remove if no tax
		if (
			not di_item.fbr_di_tax_rate
			and not di_item.fbr_di_sales_tax
			and not di_item.fbr_di_further_tax
			and not di_item.fbr_di_extra_tax
		):
			invoice.remove(di_item)

	for i, di_item in enumerate(invoice.fbr_di_items):
		di_item.idx = i + 1


def get_invoice_data(invoice):
	invoice_data = frappe._dict()

	# Invoice Details
	invoice_data.invoiceType = invoice.fbr_di_invoice_type
	invoice_data.invoiceDate = cstr(getdate(invoice.posting_date))
	invoice_data.invoiceRefNo = invoice.fbr_di_invoice_ref_no

	# Seller / Company Details
	invoice_data.sellerNTNCNIC = format_ntn_cnic(ntn=frappe.get_cached_value("Company", invoice.company, "tax_id"))
	invoice_data.sellerBusinessName = invoice.company
	invoice_data.sellerProvince = invoice.fbr_di_seller_province
	invoice_data.sellerAddress = format_address(invoice.company_address_display)

	# Customer Details
	invoice_data.buyerNTNCNIC = format_ntn_cnic(ntn=invoice.tax_id, cnic=invoice.tax_cnic)
	invoice_data.buyerBusinessName = invoice.bill_to_name or invoice.customer_name
	invoice_data.buyerProvince = invoice.fbr_di_buyer_province
	invoice_data.buyerAddress = format_address(invoice.address_display)
	invoice_data.buyerRegistrationType = invoice.fbr_di_buyer_registration_type

	# Items
	invoice_data['items'] = []
	for di_item in invoice.fbr_di_items:
		item_data = frappe._dict()
		invoice_data['items'].append(item_data)

		item = invoice.getone('items', {'name': di_item.fbr_di_item_reference})
		if not item:
			frappe.throw(_("Could not find reference to line item FBR Digital Invoice Item Row #{0} Item Code {1}").format(di_item.idx, di_item.item_code))

		# Product Details
		item_data.hsCode = di_item.fbr_di_hs_code
		item_data.productDescription = di_item.fbr_di_item_name
		item_data.quantity = flt(di_item.fbr_di_quantity)
		item_data.uoM = di_item.fbr_di_uom

		# Amounts and Taxes
		item_data.valueSalesExcludingST = flt(di_item.fbr_di_sale_value)
		item_data.fixedNotifiedValueOrRetailPrice = flt(di_item.fbr_di_retail_value)

		item_data.rate = di_item.get_formatted("fbr_di_tax_rate")
		item_data.salesTaxApplicable = flt(di_item.fbr_di_sales_tax)
		item_data.salesTaxWithheldAtSource = flt(di_item.fbr_di_sales_tax_withheld)
		item_data.extraTax = flt(di_item.fbr_di_extra_tax)
		item_data.furtherTax = flt(di_item.fbr_di_further_tax)
		item_data.fedPayable = flt(di_item.fbr_di_fed_tax)

		item_data.discount = flt(di_item.fbr_di_discount)
		item_data.totalValues = flt(di_item.fbr_di_total_value)

		# Sale Type
		item_data.sroScheduleNo = di_item.fbr_di_sro_schedule_no
		item_data.sroItemSerialNo = di_item.fbr_di_sro_serial_no
		item_data.saleType = di_item.fbr_di_sale_type

	return invoice_data


def post_fbr_di_invoice(invoice, ignore_connection_error=False, auto_commit=True):
	if not check_fbr_di_enabled():
		return
	if not invoice.meta.has_field('is_fbr_di_invoice'):
		return
	if not cint(invoice.get('is_fbr_di_invoice')):
		return
	if invoice.docstatus != 1:
		return
	if invoice.fbr_di_invoice_no:
		return

	invoice_data = get_invoice_data(invoice)
	json_data = json.dumps(invoice_data)

	invoice_number = push_invoice_data(invoice_data, invoice.name, ignore_connection_error=ignore_connection_error)

	if invoice_number:
		qrcode_svg = get_invoice_qrcode_svg(invoice_number)

		invoice.db_set({
			'fbr_di_invoice_no': invoice_number,
			'fbr_di_qrcode': qrcode_svg,
			'fbr_di_json_data': json_data,
		})

		invoice.notify_update()
		if auto_commit:
			frappe.db.commit()

	return invoice_number


def push_invoice_data(data, sales_invoice, ignore_connection_error=False):
	invoice_number = None

	fbr_di_settings = frappe.get_cached_doc("FBR Digital Invoicing Settings", None)

	if fbr_di_settings.use_sandbox:
		url = "https://gw.fbr.gov.pk/di_data/v1/di/postinvoicedata_sb"
		data["scenarioId"] = get_sandbox_scenario_id(data)
	else:
		url = "https://gw.fbr.gov.pk/di_data/v1/di/postinvoicedata"

	headers = {"Content-Type": "application/json"}
	if not fbr_di_settings.security_token:
		frappe.throw(_("Please set 'Security Token' in FBR Digital Invoicing Settings"))

	security_token = fbr_di_settings.get_password("security_token")
	headers["Authorization"] = "Bearer {0}".format(security_token)

	try:
		r = requests.post(url, json=data, headers=headers, timeout=10)
		r.raise_for_status()

		response_json = r.json()

		invoice_number = response_json.get('invoiceNumber')
		parent_response = response_json.get('validationResponse', {})
		parent_status_code = parent_response.get('statusCode')
		parent_error_message = parent_response.get('error')
		parent_error_code = parent_response.get('error_code')

		# Parent Level Error Message
		if parent_error_message or parent_error_code:
			log_fbr_di_request("Failed", sales_invoice, data, invoice_number, r,
				error_type="FBR DI Error")
			frappe.throw(_("An error occurred while generating <b>FBR Digital Invoice</b>:<br>{0}").format(
				parent_error_message or parent_error_code
			), exc=FBRResponseError)

		# Row Level Errors
		child_responses = parent_response.get("invoiceStatuses", [])
		for child_response in child_responses:
			child_idx = child_response.get('itemSNo')
			child_status_code = child_response.get('statusCode')
			child_error_message = child_response.get('error')
			child_error_code = child_response.get('error_code')

			if child_error_message or child_error_code:
				log_fbr_di_request("Failed", sales_invoice, data, invoice_number, r,
					error_type="FBR DI Error")
				frappe.throw(_("An error occurred while generating <b>FBR Digital Invoice</b>:<br>FBR DI Row #{0}: {1}").format(
					child_idx, child_error_message or child_error_code
				), exc=FBRResponseError)

			if child_status_code != '00':
				log_fbr_di_request("Failed", sales_invoice, data, invoice_number, r,
					error_type="Invalid Response Code")
				frappe.throw(_("Received an invalid response while generating <b>FBR Digital Invoice</b> on FBR DI Row #{0}").format(
					child_idx
				), exc=FBRResponseError)

		# Parent Level Invalid Status Code
		if parent_status_code != '00':
			log_fbr_di_request("Failed", sales_invoice, data, invoice_number, r,
				error_type="Invalid Response Code")
			frappe.throw(_("Received an invalid response while generating <b>FBR Digital Invoice</b>"),
				exc=FBRResponseError)

		# Missing Invoice Number
		if not invoice_number or invoice_number == 'Not Available':
			log_fbr_di_request("Failed", sales_invoice, data, invoice_number, r,
				error_type="Invoice Number Not Available")
			frappe.throw(_("FBR Digital Invoice Number was not provided by <b>FBR Digital Invoicing Service</b>"),
				exc=FBRResponseError)

	except requests.exceptions.ConnectionError as err:
		log_fbr_di_request("Failed", sales_invoice, data, invoice_number,
			error_type="Connection Error")
		frappe.flags.fbr_di_connection_error = True
		if not ignore_connection_error:
			frappe.throw(_("Could not connect to <b>FBR Digital Invoicing Service</b>:<br>{0}").format(
				err
			), exc=FBRConnectionError)

	except requests.exceptions.Timeout as err:
		log_fbr_di_request("Failed", sales_invoice, data, invoice_number,
			error_type="Connection Timeout")
		frappe.flags.fbr_di_connection_error = True
		if not ignore_connection_error:
			frappe.throw(_("Connection to <b>FBR Digital Invoicing Service</b> timed out:<br>{0}").format(
				err
			), exc=FBRConnectionError)

	except requests.exceptions.HTTPError as err:
		log_fbr_di_request("Failed", sales_invoice, data, invoice_number,
			error_type="HTTP Error")
		frappe.throw(_("An HTTP error occurred while connecting to the <b>FBR Digital Invoicing Service</b>:<br>{0}").format(
			err
		), exc=FBRRequestError)

	except requests.exceptions.RequestException as err:
		log_fbr_di_request("Failed", sales_invoice, data, invoice_number,
			error_type="Request Error")
		frappe.throw(_("Request to <b>FBR Digital Invoicing Service</b> failed:<br>{0}").format(
			err
		), exc=FBRRequestError)

	else:
		log_fbr_di_request("Completed", sales_invoice, data, invoice_number, r)

	return invoice_number


def get_invoice_type_and_ref(invoice):
	invoice_ref_no = ""
	if cint(invoice.is_return):
		if invoice.return_against:
			invoice_ref_no = frappe.db.get_value("Sales Invoice", invoice.return_against, "fbr_di_invoice_no", cache=True)

		return 'Credit Note', invoice_ref_no
	else:
		return 'Sale Invoice', invoice_ref_no


def get_item_sale_type(item):
	if item.apply_taxes_on_retail:
		return " 3rd Schedule Goods "
	else:
		return "Goods at standard rate (default)"


def get_item_qty_and_uom(item):
	qty = flt(item.qty)
	use_uom = item.uom
	if not use_uom:
		return qty, DEFAULT_UOM

	uom_doc = frappe.get_cached_doc("UOM", use_uom)
	if uom_doc.fbr_use_alt_uom:
		qty = flt(item.alt_uom_qty)
		alt_uom = item.alt_uom or item.uom
		if alt_uom:
			use_uom = alt_uom
			uom_doc = frappe.get_cached_doc("UOM", use_uom)

	return qty, uom_doc.fbr_uom or DEFAULT_UOM


def get_province_from_address(address):
	if not address:
		return ""

	return frappe.db.get_value("Address", address, "state", cache=True)


def get_sandbox_scenario_id(data):
	scenario_id = ""

	if any(d.get("saleType") == " 3rd Schedule Goods " for d in data.get("items")):
		return "SN008"
	elif any(d.get("saleType") == "Goods at standard rate (default)" for d in data.get("items")):
		if data.get("buyerRegistrationType") == "Registered":
			scenario_id = "SN001"
		elif data.get("buyerRegistrationType") == "Unregistered":
			scenario_id = "SN002"

	return scenario_id


def format_ntn_cnic(ntn=None, cnic=None):
	if ntn:
		return ntn[:7]
	elif cnic:
		return cnic.replace("-", "")
	else:
		return ""


def format_address(address):
	address = cstr(address)
	address = address.replace("<br/>", " ")
	address = address.replace("<br />", " ")
	address = address.replace("<br>", " ")
	address = address.replace("\n", " ")
	address = address.replace("\r", " ")
	address = strip_html(address)
	return clean_whitespace(address)


def log_fbr_di_request(
	status,
	sales_invoice,
	data,
	invoice_number=None,
	response=None,
	error_type=None,
):
	return log_fbr_request(
		service="FBR DI",
		status=status,
		sales_invoice=sales_invoice,
		data=data,
		invoice_number=invoice_number,
		response=response,
		error_type=error_type,
	)


@frappe.whitelist()
def sync_fbr_di_invoice(sales_invoice):
	check_fbr_di_enabled(throw=True)

	invoice = frappe.get_doc("Sales Invoice", sales_invoice)
	invoice.check_permission("submit")

	invoice_number = post_fbr_di_invoice(invoice, ignore_connection_error=False, auto_commit=True)
	if invoice_number:
		frappe.msgprint(_("FBR Digital Invoice Number {0} generated for Sales Invoice {1}")
			.format(frappe.bold(invoice_number), invoice.name))
	else:
		frappe.msgprint(_("FBR Digital Invoice Number could not be generated"))

	return invoice_number


# called by scheduler
def post_fbr_di_invoices_without_number():
	if not check_fbr_di_enabled():
		return

	failed_invoices = frappe.db.sql_list("""
		select name
		from `tabSales Invoice`
		where docstatus = 1 and is_fbr_di_invoice = 1 and (fbr_di_invoice_no = '' or fbr_di_invoice_no is null)
		order by posting_date, posting_time, creation
		limit 100
		for update
	""")

	for name in failed_invoices:
		invoice = frappe.get_doc("Sales Invoice", name)
		try:
			post_fbr_di_invoice(invoice, ignore_connection_error=False, auto_commit=True)
		except FBRRequestError:
			frappe.db.rollback()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				message=frappe.get_traceback(),
				title="FBR Digital Invoice {0} Failed".format(invoice.name),
				reference_doctype="Sales Invoice",
				reference_name=name
			)
