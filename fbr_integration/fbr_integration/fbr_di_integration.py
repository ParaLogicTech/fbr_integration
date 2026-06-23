import frappe
from frappe import _
from frappe.utils import cint, flt, cstr, getdate, strip_html, clean_whitespace
from erpnext.stock.doctype.item.item import convert_item_uom_for
from erpnext.setup.doctype.uom_conversion_factor.uom_conversion_factor import get_uom_conv_factor
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

di_item_sum_fields = [
	"fbr_di_quantity",
	"fbr_di_sale_value",
	"fbr_di_retail_value",
	"fbr_di_discount",
	"fbr_di_sales_tax",
	"fbr_di_extra_tax",
	"fbr_di_further_tax",
	"fbr_di_sales_tax_withheld",
	"fbr_di_fed_tax",
	"fbr_di_total_value",
]


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


@frappe.whitelist()
def refresh_fbr_di_values(invoice):
	if isinstance(invoice, str):
		invoice = frappe.parse_json(invoice)

	invoice = frappe.get_doc(invoice)

	invoice.check_permission("write")
	if invoice.docstatus != 0:
		frappe.throw(_("Invoice is not in draft"))

	invoice.run_method("validate_fbr_di_invoice")

	return invoice


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

	if should_validate_on_submit():
		validate_fbr_di_invoice_data(invoice)
	if is_fbr_di_realtime():
		_sync_fbr_di_invoice.enqueue(sales_invoice=invoice.name, ignore_permissions=True, enqueue_after_commit=True)


def determine_is_fbr_di(invoice):
	enable_fbr_di = cint(frappe.get_cached_value("FBR Digital Invoicing Settings", None, "enable_fbr_di"))
	fbr_di_starting_date = frappe.get_cached_value("FBR Digital Invoicing Settings", None, "starting_date")

	if not enable_fbr_di:
		return 0
	if not cint(invoice.get('has_stin')):
		return 0
	if fbr_di_starting_date and getdate(invoice.posting_date) < getdate(fbr_di_starting_date):
		return 0

	if cint(invoice.get('is_return')):
		return 0

	return 1


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


def should_validate_on_submit():
	if is_fbr_di_realtime():
		return True

	return cint(frappe.get_cached_value("FBR Digital Invoicing Settings", None, "validate_on_submit"))


def is_fbr_di_realtime():
	if is_fbr_di_paused():
		return False

	return cint(frappe.get_cached_value("FBR Digital Invoicing Settings", None, "is_realtime"))


def is_fbr_di_paused():
	return cint(frappe.get_cached_value("FBR Digital Invoicing Settings", None, "pause_posting"))


def validate_is_fbr_di(invoice):
	for di_item in invoice.fbr_di_items:
		if not di_item.fbr_di_hs_code:
			item = invoice.getone('items', {'name': di_item.fbr_di_item_reference})
			if not item:
				continue

			frappe.msgprint(_("Row #{0}: Could not determine HS Code for FBR Digital Invoicing for Item {1}").format(
				item.idx, frappe.bold(item.item_code)
			), raise_exception=invoice.docstatus == 1 and should_validate_on_submit())


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
	invoice.fbr_di_invoice_type, invoice.fbr_di_invoice_ref_no = get_invoice_type_and_ref(invoice)
	invoice.fbr_di_buyer_registration_type = "Registered" if invoice.tax_strn else "Unregistered"
	invoice.fbr_di_seller_province = get_province_from_address(invoice.company_address)
	invoice.fbr_di_buyer_province = get_province_from_address(invoice.customer_address)

	# Items
	make_fbr_di_items(invoice)
	merge_fbr_di_items(invoice)
	postprocess_fbr_di_items(invoice)


def make_fbr_di_items(invoice):
	sales_tax_account = frappe.get_cached_value('Company', invoice.company, "sales_tax_account")
	extra_tax_account = frappe.get_cached_value('Company', invoice.company, "extra_tax_account")
	further_tax_account = frappe.get_cached_value('Company', invoice.company, "further_tax_account")

	if not sales_tax_account:
		frappe.throw(_("Please set Sales Tax Account in {0} to calculate FBR Digital Invoice Data").format(
			frappe.get_desk_link("Company", invoice.company)
		))

	# Create Item Row ID Map
	item_map = {}
	existing_di_item_map = {}
	for d in invoice.items:
		item_map[d.name] = d
	for d in invoice.get('fbr_di_items'):
		if d.fbr_di_item_reference:
			existing_di_item_map[d.fbr_di_item_reference] = d

	invoice.fbr_di_items = []
	for item in invoice.items:
		existing_di_item = existing_di_item_map.get(item.name)
		di_item = invoice.append('fbr_di_items', existing_di_item)

		# Reference to line item
		di_item.fbr_di_item_reference = item.name

		# Item Code / Type
		di_item.fbr_di_item_name = clean_string(item.item_name)
		di_item.fbr_di_hs_code = get_item_hs_code(item, invoice)
		di_item.fbr_di_sale_type = get_item_sale_type(item)

		# Qty / Amounts
		qty, uom = get_item_qty_and_uom(item, di_item.fbr_di_hs_code)
		di_item.fbr_di_quantity = flt(qty)
		di_item.fbr_di_uom = uom
		di_item.fbr_di_sale_value = flt(item.base_net_amount)
		di_item.fbr_di_retail_value = flt(item.base_taxable_amount) if cint(item.apply_taxes_on_retail) else 0
		di_item.fbr_di_discount = flt(item.base_tax_exclusive_total_discount)

		# Taxes
		sales_tax_details = get_item_tax_details(item, invoice, sales_tax_account)
		extra_tax_details = get_item_tax_details(item, invoice, extra_tax_account)
		further_tax_details = get_item_tax_details(item, invoice, further_tax_account)

		di_item.fbr_di_tax_rate = flt(sales_tax_details.rate, di_item.precision('fbr_di_tax_rate'))
		di_item.fbr_di_sales_tax = flt(sales_tax_details.tax_amount_after_discount_amount)
		di_item.fbr_di_extra_tax = flt(extra_tax_details.tax_amount_after_discount_amount)
		di_item.fbr_di_further_tax = flt(further_tax_details.tax_amount_after_discount_amount)

		di_item.fbr_di_sales_tax_withheld = 0
		di_item.fbr_di_fed_tax = 0

		di_item.fbr_di_total_value = flt(
			di_item.fbr_di_sale_value + di_item.fbr_di_sales_tax + di_item.fbr_di_further_tax + di_item.fbr_di_extra_tax
		)

		# reverse sign for credit note
		if invoice.is_return:
			for f in di_item_sum_fields:
				di_item.set(f, -1 * di_item.get(f))

		# SRO
		di_item.fbr_di_sro_schedule_no = item.fbr_sro_schedule_no
		di_item.fbr_di_sro_serial_no = item.fbr_sro_serial_no

		# Remove if no tax
		if (
			not di_item.fbr_di_tax_rate
			and not di_item.fbr_di_sales_tax
			and not di_item.fbr_di_further_tax
			and not di_item.fbr_di_extra_tax
			and di_item.fbr_di_sale_type not in ("Goods at zero-rate", "Exempt goods")
		):
			invoice.remove(di_item)

	for i, di_item in enumerate(invoice.fbr_di_items):
		di_item.idx = i + 1


def merge_fbr_di_items(invoice):
	def get_group_key(it):
		key = [
			cstr(it.fbr_di_item_reference if not it.fbr_di_hs_code else ""),
			cstr(it.fbr_di_hs_code),
			cstr(it.fbr_di_uom),
			cstr(it.fbr_di_sale_type),
			flt(it.fbr_di_tax_rate),
			cstr(it.fbr_di_sro_schedule_no),
			cstr(it.fbr_di_sro_serial_no),
		]

		if not merge_hs_codes:
			key.append(cstr(it.fbr_di_item_name))

		return tuple(key)

	merge_hs_codes = cint(frappe.get_cached_value("FBR Digital Invoicing Settings", None, "merge_hs_codes"))

	group_item_data = {}

	for di_item in invoice.fbr_di_items:
		group_key = get_group_key(di_item)
		group_item = group_item_data.setdefault(group_key, frappe._dict())
		for f in di_item_sum_fields:
			group_item[f] = group_item.get(f, 0) + flt(di_item.get(f))

		if merge_hs_codes:
			hs_code_description = frappe.get_cached_value("Customs Tariff Number", di_item.fbr_di_hs_code, "description")
			hs_code_description = hs_code_description or di_item.fbr_di_item_name
			group_item["fbr_di_item_name"] = clean_string(hs_code_description)

	duplicate_list = []
	count = 0
	for di_item in invoice.fbr_di_items:
		group_key = get_group_key(di_item)
		if group_key in group_item_data.keys():
			count += 1
			di_item.update(group_item_data[group_key])
			di_item.idx = count
			del group_item_data[group_key]
		else:
			duplicate_list.append(di_item)

	for di_item in duplicate_list:
		invoice.remove(di_item)

	# handle same hscode/description
	visit_count = {}
	for di_item in invoice.fbr_di_items:
		key = (cstr(di_item.fbr_di_hs_code), cstr(di_item.fbr_di_item_name))
		if key in visit_count:
			visit_count[key] += 1
			di_item.fbr_di_item_name = f"{di_item.fbr_di_item_name} ({cstr(visit_count[key])})"
		else:
			visit_count[key] = 1


def postprocess_fbr_di_items(invoice):
	# Rounding
	for di_item in invoice.fbr_di_items:
		invoice.round_floats_in(di_item)

	# FBR rounding error fix
	for di_item in invoice.fbr_di_items:
		if not di_item.fbr_di_tax_rate:
			continue

		is_3rd_schedule = di_item.fbr_di_sale_type == " 3rd Schedule Goods "

		calculated_taxable_value = flt(di_item.fbr_di_sales_tax / di_item.fbr_di_tax_rate * 100, di_item.precision('fbr_di_sales_tax'))
		actual_taxable_value = di_item.fbr_di_retail_value if is_3rd_schedule else di_item.fbr_di_sale_value

		if abs(calculated_taxable_value - actual_taxable_value) < 0.1:
			if is_3rd_schedule:
				di_item.fbr_di_retail_value = calculated_taxable_value
			else:
				di_item.fbr_di_sale_value = calculated_taxable_value

			di_item.fbr_di_total_value = flt(
				di_item.fbr_di_sale_value + di_item.fbr_di_sales_tax + di_item.fbr_di_further_tax + di_item.fbr_di_extra_tax,
				di_item.precision('fbr_di_total_value')
			)


def get_invoice_data(invoice):
	invoice_data = frappe._dict()

	# Invoice Details
	invoice_data.invoiceType = invoice.fbr_di_invoice_type
	invoice_data.invoiceDate = cstr(getdate(invoice.posting_date))
	invoice_data.invoiceRefNo = invoice.fbr_di_invoice_ref_no
	invoice_data.sourceInvoiceNo = invoice.name

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
			frappe.throw(_("Could not find reference to line item FBR Digital Invoice Item Row #{0}").format(di_item.idx))

		# Product Details
		item_data.hsCode = di_item.fbr_di_hs_code
		item_data.productDescription = di_item.fbr_di_item_name
		item_data.quantity = flt(di_item.fbr_di_quantity)
		item_data.uoM = di_item.fbr_di_uom

		# Amounts and Taxes
		item_data.valueSalesExcludingST = flt(di_item.fbr_di_sale_value)
		item_data.fixedNotifiedValueOrRetailPrice = flt(di_item.fbr_di_retail_value)

		if not di_item.fbr_di_sales_tax and di_item.fbr_di_sale_type == "Exempt goods":
			item_data.rate = "Exempt"
		else:
			item_data.rate = di_item.get_formatted("fbr_di_tax_rate")

		item_data.salesTaxApplicable = flt(di_item.fbr_di_sales_tax)
		item_data.salesTaxWithheldAtSource = flt(di_item.fbr_di_sales_tax_withheld)
		item_data.extraTax = flt(di_item.fbr_di_extra_tax) or ""
		item_data.furtherTax = flt(di_item.fbr_di_further_tax)
		item_data.fedPayable = flt(di_item.fbr_di_fed_tax)

		item_data.discount = flt(di_item.fbr_di_discount)
		item_data.totalValues = flt(di_item.fbr_di_total_value)

		# Sale Type
		item_data.sroScheduleNo = di_item.fbr_di_sro_schedule_no
		item_data.sroItemSerialNo = di_item.fbr_di_sro_serial_no
		item_data.saleType = di_item.fbr_di_sale_type

	if cint(frappe.get_cached_value("FBR Digital Invoicing Settings", None, "use_sandbox")):
		invoice_data["scenarioId"] = get_sandbox_scenario_id(invoice_data, invoice)

	return invoice_data


def validate_fbr_di_invoice_data(invoice, method=None):
	if not check_fbr_di_enabled():
		return
	if not invoice.meta.has_field('is_fbr_di_invoice'):
		return
	if not cint(invoice.get('is_fbr_di_invoice')):
		return

	invoice_data = get_invoice_data(invoice)
	push_invoice_data(invoice_data, invoice.name, for_validate=True)


def post_fbr_di_invoice_data(invoice, auto_commit=True):
	if not check_fbr_di_enabled():
		return None
	if not invoice.meta.has_field('is_fbr_di_invoice'):
		return None
	if not cint(invoice.get('is_fbr_di_invoice')):
		return None
	if invoice.fbr_di_invoice_no:
		return invoice.fbr_di_invoice_no
	if invoice.docstatus != 1:
		return None
	if is_fbr_di_paused():
		return None

	invoice_data = get_invoice_data(invoice)
	json_data = json.dumps(invoice_data)

	invoice_number = push_invoice_data(invoice_data, invoice.name, for_validate=False, auto_commit=auto_commit)

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


def push_invoice_data(data, sales_invoice, for_validate, auto_commit=False):
	invoice_number = None

	fbr_di_settings = frappe.get_cached_doc("FBR Digital Invoicing Settings", None)

	if fbr_di_settings.use_sandbox:
		if for_validate:
			url = "https://gw.fbr.gov.pk/di_data/v1/di/validateinvoicedata_sb"
		else:
			url = "https://gw.fbr.gov.pk/di_data/v1/di/postinvoicedata_sb"
	else:
		if for_validate:
			url = "https://gw.fbr.gov.pk/di_data/v1/di/validateinvoicedata"
		else:
			url = "https://gw.fbr.gov.pk/di_data/v1/di/postinvoicedata"

	headers = {"Content-Type": "application/json"}
	if not fbr_di_settings.security_token:
		frappe.throw(_("Please set 'Security Token' in FBR Digital Invoicing Settings"))

	security_token = fbr_di_settings.get_password("security_token")
	headers["Authorization"] = "Bearer {0}".format(security_token)

	log = log_fbr_di_request("Queued", sales_invoice, data, invoice_number,
		for_validate=for_validate, auto_commit=auto_commit)

	try:
		r = requests.post(url, json=data, headers=headers, timeout=60 if for_validate else 120)
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
				error_type="FBR DI Error", for_validate=for_validate, existing_log=log, auto_commit=auto_commit)
			frappe.throw(_("An error occurred while processing <b>FBR Digital Invoice</b>:<br>{0}").format(
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
					error_type="FBR DI Error", for_validate=for_validate, existing_log=log, auto_commit=auto_commit)
				frappe.throw(_("An error occurred while processing <b>FBR Digital Invoice</b>:<br>FBR DI Row #{0}: {1}").format(
					child_idx, child_error_message or child_error_code
				), exc=FBRResponseError)

			if child_status_code != '00':
				log_fbr_di_request("Failed", sales_invoice, data, invoice_number, r,
					error_type="Invalid Response Code", for_validate=for_validate, existing_log=log, auto_commit=auto_commit)
				frappe.throw(_("Received an invalid response while processing <b>FBR Digital Invoice</b> on FBR DI Row #{0}").format(
					child_idx
				), exc=FBRResponseError)

		# Parent Level Invalid Status Code
		if parent_status_code != '00':
			log_fbr_di_request("Failed", sales_invoice, data, invoice_number, r,
				error_type="Invalid Response Code", for_validate=for_validate, existing_log=log, auto_commit=auto_commit)
			frappe.throw(_("Received an invalid response while processing <b>FBR Digital Invoice</b>"),
				exc=FBRResponseError)

		# Missing Invoice Number
		if not for_validate and not invoice_number or invoice_number == 'Not Available':
			log_fbr_di_request("Failed", sales_invoice, data, invoice_number, r,
				error_type="Invoice Number Not Available", for_validate=for_validate, existing_log=log, auto_commit=auto_commit)
			frappe.throw(_("FBR Digital Invoice Number was not provided by <b>FBR Digital Invoicing Service</b>"),
				exc=FBRResponseError)

	except requests.exceptions.ConnectionError as err:
		log_fbr_di_request("Failed", sales_invoice, data, invoice_number,
			error_type="Connection Error", for_validate=for_validate, existing_log=log, auto_commit=auto_commit)
		frappe.throw(_("Could not connect to <b>FBR Digital Invoicing Service</b>:<br>{0}").format(
			err
		), exc=FBRConnectionError)

	except requests.exceptions.Timeout as err:
		log_fbr_di_request("Failed", sales_invoice, data, invoice_number,
			error_type="Connection Timeout", for_validate=for_validate, existing_log=log, auto_commit=auto_commit)
		frappe.throw(_("Connection to <b>FBR Digital Invoicing Service</b> timed out:<br>{0}").format(
			err
		), exc=FBRConnectionError)

	except requests.exceptions.HTTPError as err:
		log_fbr_di_request("Failed", sales_invoice, data, invoice_number,
			error_type="HTTP Error", for_validate=for_validate, existing_log=log, auto_commit=auto_commit)
		frappe.throw(_("An HTTP error occurred while connecting to the <b>FBR Digital Invoicing Service</b>:<br>{0}").format(
			err
		), exc=FBRRequestError)

	except requests.exceptions.RequestException as err:
		log_fbr_di_request("Failed", sales_invoice, data, invoice_number,
			error_type="Request Error", for_validate=for_validate, existing_log=log, auto_commit=auto_commit)
		frappe.throw(_("Request to <b>FBR Digital Invoicing Service</b> failed:<br>{0}").format(
			err
		), exc=FBRRequestError)

	else:
		log_fbr_di_request("Completed", sales_invoice, data, invoice_number, r,
			for_validate=for_validate, existing_log=log, auto_commit=auto_commit)

	return invoice_number


def get_invoice_type_and_ref(invoice):
	if cint(invoice.is_return):
		invoice_ref_no = ""
		if invoice.return_against:
			invoice_ref_no = frappe.db.get_value("Sales Invoice", invoice.return_against, "fbr_di_invoice_no", cache=True)

		return 'Credit Note', invoice_ref_no
	else:
		return 'Sale Invoice', None


def get_item_sale_type(item):
	if item.get("fbr_sales_tax_type"):
		return item.get("fbr_sales_tax_type")
	elif item.apply_taxes_on_retail:
		return " 3rd Schedule Goods "
	else:
		return "Goods at standard rate (default)"


def get_item_qty_and_uom(item, customs_tariff_number):
	if item.uom:
		qty = flt(item.qty)
		use_uom = item.uom
	else:
		qty = flt(item.stock_qty)
		use_uom = flt(item.stock_uom)

	validate_conversion = item.docstatus == 1

	if customs_tariff_number:
		tariff_doc = frappe.get_cached_doc("Customs Tariff Number", customs_tariff_number)

		if tariff_doc.fbr_qty_uom == "Convert to UOM" and tariff_doc.fbr_convert_uom:
			qty, use_uom = convert_uom(
				qty,
				use_uom,
				tariff_doc.fbr_convert_uom,
				item.idx,
				item_code=item.item_code,
				throw=validate_conversion,
			)

		elif tariff_doc.fbr_qty_uom == "Contents UOM Qty":
			qty = flt(item.alt_uom_qty)
			use_uom = item.alt_uom or use_uom

		elif tariff_doc.fbr_qty_uom == "Stock UOM Qty":
			qty = flt(item.stock_qty)
			use_uom = item.stock_uom or use_uom

		elif tariff_doc.fbr_qty_uom == "Net Weight":
			if flt(item.get("net_weight")) and item.weight_uom:
				if tariff_doc.fbr_convert_uom:
					qty, use_uom = convert_uom(item.get("net_weight"), item.weight_uom, tariff_doc.fbr_convert_uom, item.idx,
						throw=validate_conversion)
				else:
					qty = flt(item.get("net_weight"))
					use_uom = item.weight_uom or use_uom
			else:
				frappe.msgprint(_("FBR Digital Invoice Item Row #{0}: Net Weight is zero or Weight UOM is missing").format(
					item.idx,
				), raise_exception=validate_conversion)

		elif tariff_doc.fbr_qty_uom == "Gross Weight":
			if flt(item.get("gross_weight")) and item.weight_uom:
				if tariff_doc.fbr_convert_uom:
					qty, use_uom = convert_uom(item.get("gross_weight"), item.weight_uom, tariff_doc.fbr_convert_uom, item.idx,
						throw=validate_conversion)
				else:
					qty = flt(item.get("gross_weight"))
					use_uom = item.weight_uom or use_uom
			else:
				frappe.msgprint(_("FBR Digital Invoice Item Row #{0}: Gross Weight is zero or Weight UOM is missing").format(
					item.idx,
				), raise_exception=validate_conversion)

	fbr_uom = frappe.get_cached_value("UOM", use_uom, "fbr_uom")
	return qty, fbr_uom or DEFAULT_UOM


def convert_uom(qty, from_uom, to_uom, idx, item_code=None, throw=False):
	qty = flt(qty)

	if item_code:
		converted_qty = convert_item_uom_for(
			qty,
			item_code,
			from_uom=from_uom,
			to_uom=to_uom,
			null_if_not_convertible=True,
		)

		if converted_qty is None:
			frappe.msgprint(_("FBR Digital Invoice Item Row #{0}: Cannot convert UOM from {1} to {2}. Please configure Conversion Factor in {3}").format(
				idx,
				frappe.bold(from_uom),
				frappe.bold(to_uom),
				frappe.get_desk_link("Item", item_code),
			), raise_exception=throw)

			return qty, from_uom

		return converted_qty, to_uom
	else:
		conversion_factor = get_uom_conv_factor(from_uom, to_uom)
		if not conversion_factor:
			frappe.msgprint(_("FBR Digital Invoice Item Row #{0}: Cannot convert UOM from {1} to {2}").format(
				idx,
				frappe.bold(from_uom),
				frappe.bold(to_uom),
			), raise_exception=throw)
			return qty, from_uom

		return qty * conversion_factor, to_uom


def get_province_from_address(address):
	if not address:
		return ""

	return frappe.db.get_value("Address", address, "state", cache=True)


def get_sandbox_scenario_id(data, invoice):
	if any(d.get("saleType") == "Goods at zero-rate" for d in data.get("items")):
		return "SN007"

	elif any(d.get("saleType") == "Exempt goods" for d in data.get("items")):
		return "SN006"

	elif any(d.get("saleType") == "Processing/Conversion of Goods" for d in data.get("items")):
		return "SN016"

	elif any(d.get("saleType") == "Goods (FED in ST Mode)" for d in data.get("items")):
		return "SN017"

	elif any(d.get("saleType") == "Goods as per SRO.297(|)/2023" for d in data.get("items")):
		return "SN024"

	elif any(d.get("saleType") == " 3rd Schedule Goods " for d in data.get("items")):
		if invoice.get("is_pos"):
			return "SN027"
		else:
			return "SN008"

	elif any(d.get("saleType") == "Goods at Reduced Rate" for d in data.get("items")):
		if invoice.get("is_pos"):
			return "SN028"
		else:
			return "SN005"

	elif any(d.get("saleType") == "Electric Vehicle" for d in data.get("items")):
		return "SN020"

	elif any(d.get("saleType") == "Goods at standard rate (default)" for d in data.get("items")):
		if invoice.get("is_pos"):
			return "SN026"
		elif data.get("buyerRegistrationType") == "Registered":
			return "SN001"
		elif data.get("buyerRegistrationType") == "Unregistered":
			return "SN002"

	return ""


def format_ntn_cnic(ntn=None, cnic=None):
	if ntn:
		return ntn[:7]
	elif cnic:
		return cnic.replace("-", "")
	else:
		return ""


def format_address(address):
	return clean_string(address, remove_html=True)


def clean_string(string, remove_html=False):
	string = cstr(string)

	string = string.replace("<br/>", " ")
	string = string.replace("<br />", " ")
	string = string.replace("<br>", " ")

	if remove_html:
		string = strip_html(string)

	string = " ".join(string.splitlines())
	return clean_whitespace(string)


def log_fbr_di_request(
	status,
	sales_invoice,
	data,
	invoice_number=None,
	response=None,
	error_type=None,
	for_validate=False,
	existing_log=None,
	auto_commit=False,
):
	if for_validate:
		return None

	return log_fbr_request(
		service="FBR DI",
		status=status,
		sales_invoice=sales_invoice,
		data=data,
		invoice_number=invoice_number,
		response=response,
		error_type=error_type,
		existing_log=existing_log,
		auto_commit=auto_commit,
		enqueue=False,
	)


@frappe.whitelist()
def sync_fbr_di_invoice(sales_invoice):
	check_fbr_di_enabled(throw=True)

	if is_fbr_di_paused():
		frappe.msgprint(_("FBR Digital Invoicing is paused. Please contact your System Administrator"))
		return None

	invoice_number = _sync_fbr_di_invoice(sales_invoice)
	if invoice_number:
		frappe.msgprint(_("FBR Digital Invoice Number {0} generated for Sales Invoice {1}").format(
			frappe.bold(invoice_number), sales_invoice
		))
	else:
		frappe.msgprint(_("FBR Digital Invoice Number could not be generated"))

	return invoice_number


@frappe.task(queue="long")
def _sync_fbr_di_invoice(sales_invoice, ignore_permissions=False):
	invoice = frappe.get_doc("Sales Invoice", sales_invoice, for_update=True)
	if not ignore_permissions:
		check_fbr_di_posting_permission(invoice)

	invoice_number = post_fbr_di_invoice_data(invoice, auto_commit=True)
	return invoice_number


def check_fbr_di_posting_permission(invoice):
	invoice.check_permission("submit")

	posting_role = frappe.get_cached_value("FBR Digital Invoicing Settings", None, "posting_role")
	if posting_role and posting_role not in frappe.get_roles():
		frappe.throw(_("You do not have permission to post FBR Digital Invoice"))


# called by scheduler
def post_fbr_di_invoices_without_number():
	if not check_fbr_di_enabled():
		return
	if not is_fbr_di_realtime():
		return
	if is_fbr_di_paused():
		return

	pending_invoices = frappe.db.sql_list("""
		select name
		from `tabSales Invoice`
		where docstatus = 1 and is_fbr_di_invoice = 1 and (fbr_di_invoice_no = '' or fbr_di_invoice_no is null)
		order by posting_date, posting_time, creation
		limit 10
		for update
	""")

	for name in pending_invoices:
		invoice = frappe.get_doc("Sales Invoice", name, for_update=True)
		try:
			post_fbr_di_invoice_data(invoice, auto_commit=True)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="FBR Digital Invoice {0} Failed".format(invoice.name),
				reference_doctype="Sales Invoice",
				reference_name=name
			)
