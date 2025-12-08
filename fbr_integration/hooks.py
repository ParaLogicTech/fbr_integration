app_name = "fbr_integration"
app_title = "FBR Integration"
app_publisher = "ParaLogic"
app_description = "FBR Integration"
app_email = "info@paralogic.io"
app_license = "gpl-3.0"

required_apps = ["ParaLogicTech/erpnext"]

doc_events = {
	"Sales Invoice": {
		"validate": [
			"fbr_integration.fbr_integration.fbr_di_integration.validate_fbr_di_invoice",
			"fbr_integration.fbr_integration.fbr_pos_integration.validate_fbr_pos_invoice",
		],
		"on_submit": [
			"fbr_integration.fbr_integration.fbr_di_integration.on_submit_fbr_di_invoice",
			"fbr_integration.fbr_integration.fbr_pos_integration.on_submit_fbr_pos_invoice",
		],
		"before_cancel": [
			"fbr_integration.fbr_integration.fbr_di_integration.before_cancel_fbr_di_invoice",
			"fbr_integration.fbr_integration.fbr_pos_integration.before_cancel_fbr_pos_invoice",
		],
	}
}

doctype_js = {
	"Sales Invoice": "overrides/sales_invoice_hooks.js",
}

scheduler_events = {
	"hourly_long": [
		"fbr_integration.fbr_integration.fbr_di_integration.post_fbr_di_invoices_without_number",
		"fbr_integration.fbr_integration.fbr_pos_integration.post_fbr_pos_invoices_without_number",
	]
}

transaction_controller_init = [
	"fbr_integration.overrides.item_details_hooks.transaction_controller_init_hook",
]

get_item_tax_template_details = [
	"fbr_integration.overrides.item_details_hooks.get_item_tax_template_details_hook",
]

fixtures = [
	{
		"doctype": "Custom Field",
		"filters": {
			"name": ["in", [
				"Sales Invoice-fbr_pos_details_tab",
				"Sales Invoice-fbr_di_details_tab",

				"Item Group-customs_tariff_number",

				"UOM-fbr_uom",
				"UOM-fbr_use_alt_uom",

				"Item Tax Template-sec_fbr",
				"Item Tax Template-fbr_sales_tax_type",
				"Item Tax Template-cb_fbr_1",
				"Item Tax Template-fbr_sro_schedule_no",
				"Item Tax Template-cb_fbr_2",
				"Item Tax Template-fbr_sro_serial_no",
			]]
		},
	},
]
