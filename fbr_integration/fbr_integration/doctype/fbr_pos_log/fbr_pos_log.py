# Copyright (c) 2025, ParaLogic and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class FBRPOSLog(Document):
	def on_trash(self):
		frappe.throw(_("Not allowed to delete FBR POS Log"))
