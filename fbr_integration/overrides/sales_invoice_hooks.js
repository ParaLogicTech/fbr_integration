frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (frm.doc.docstatus == 1 && frm.has_perm("submit")) {
			if (cint(frm.doc.is_fbr_di_invoice) && !frm.doc.fbr_di_invoice_no) {
				frm.add_custom_button(__('Sync FBR Digital Invoice'), () => {
					frm.events.sync_fbr_di_invoice(frm);
				}, __("FBR Integration"));
			}
			if (cint(frm.doc.is_fbr_pos_invoice) && !frm.doc.fbr_pos_invoice_no) {
				frm.add_custom_button(__('Sync FBR POS Invoice'), () => {
					frm.events.sync_fbr_pos_invoice(frm);
				}, __("FBR Integration"));
			}
		}
	},

	sync_fbr_di_invoice(frm) {
		return frappe.call({
			method: "fbr_integration.fbr_integration.fbr_di_integration.sync_fbr_di_invoice",
			args: {
				sales_invoice: frm.doc.name
			},
			freeze: 1,
			freeze_message: __("Syncing with FBR Digital Invoicing Service"),
			callback: (r) => {
				if (!r.exc && r.message) {
					frm.reload_doc();
				}
			}
		});
	},

	sync_fbr_pos_invoice(frm) {
		return frappe.call({
			method: "fbr_integration.fbr_integration.fbr_pos_integration.sync_fbr_pos_invoice",
			args: {
				sales_invoice: frm.doc.name
			},
			freeze: 1,
			freeze_message: __("Syncing with FBR POS Service"),
			callback: (r) => {
				if (!r.exc && r.message) {
					frm.reload_doc();
				}
			}
		});
	}
});
