# -*- coding: utf-8 -*-
# Copyright (c) 2020, Youssef Restom and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime, nowdate, flt, cint, get_datetime_str, add_days
from frappe import _
from erpnext.accounts.party import get_party_account
from erpnext.stock.get_item_details import get_item_details
import json
from frappe.utils.background_jobs import enqueue
from posawesome import console


@frappe.whitelist()
def get_opening_dialog_data():
    data = {}
    data["companys"] = frappe.get_list(
        "Company", limit_page_length=0, order_by='name')
    data["pos_profiles_data"] = frappe.get_list(
        "POS Profile", filters = {"disabled" : 0}, fields=["name", "company"], limit_page_length=0, order_by='name')

    pos_profiles_list = []
    for i in data["pos_profiles_data"]:
        pos_profiles_list.append(i.name)

    payment_method_table = "POS Payment Method" if get_version() == 13 else "Sales Invoice Payment"
    data["payments_method"] = frappe.get_list(
        payment_method_table,
        filters={"parent": ["in", pos_profiles_list]},
        fields=["*"],
        limit_page_length=0,
        order_by='parent'
    )

    return data


@frappe.whitelist()
def create_opening_voucher(pos_profile, company, balance_details):
    balance_details = json.loads(balance_details)

    new_pos_opening = frappe.get_doc({
        'doctype': 'POS Opening Shift',
        "period_start_date": frappe.utils.get_datetime(),
        "posting_date": frappe.utils.getdate(),
        "user": frappe.session.user,
        "pos_profile": pos_profile,
        "company": company,
    })
    new_pos_opening.set("balance_details", balance_details)
    new_pos_opening.submit()
    
    data = {}
    data["pos_opening_shift"] = new_pos_opening.as_dict()
    update_opening_shift_data(data, new_pos_opening.pos_profile)
    return data


@frappe.whitelist()
def check_opening_shift(user):
    open_vouchers = frappe.db.get_all("POS Opening Shift",
                                      filters={
                                          "user": user,
                                          "pos_closing_shift": ["in", ["", None]],
                                          "docstatus": 1,
                                          "status": "Open"
                                      },
                                      fields=["name",
                                              "pos_profile"],
                                      order_by="period_start_date desc"
                                      )
    data = ""
    if len(open_vouchers) > 0:
        data = {}
        data["pos_opening_shift"] = frappe.get_doc(
            "POS Opening Shift", open_vouchers[0]["name"])
        update_opening_shift_data(data, open_vouchers[0]["pos_profile"])
    return data

def update_opening_shift_data(data,pos_profile):
    data["pos_profile"] = frappe.get_doc(
        "POS Profile", pos_profile)
    data["company"] = frappe.get_doc(
        "Company", data["pos_profile"].company)
    allow_negative_stock = frappe.get_value(
        "Stock Settings", None, "allow_negative_stock")
    data["stock_settings"] = {}
    data["stock_settings"].update({
        "allow_negative_stock": allow_negative_stock
    })

@frappe.whitelist()
def get_items(pos_profile):
    pos_profile = json.loads(pos_profile)
    price_list = pos_profile.get("selling_price_list")

    item_groups_list = []
    if pos_profile.get("item_groups"):
        for group in pos_profile.get("item_groups"):
            if not frappe.db.exists('Item Group', group.get("item_group")):
                item_group = get_root_of(group.get("item_group"))
                item_groups_list.append(item_group)
            else:
                item_groups_list.append(group.get("item_group"))

    conditon = ""
    if len(item_groups_list) > 0:
        if len(item_groups_list) == 1:
            conditon = "AND item_group = '{0}'".format(item_groups_list[0])
        else:
            conditon = "AND item_group in {0}".format(tuple(item_groups_list))

    result = []

    items_data = frappe.db.sql("""
        SELECT
            name AS item_code,
            item_name,
            description,
            stock_uom,
            image,
            is_stock_item,
            has_variants,
            variant_of,
            item_group,
            idx as idx,
            has_batch_no,
            has_serial_no
        FROM
            `tabItem`
        WHERE
            disabled = 0
                AND is_sales_item = 1
                AND is_fixed_asset = 0
                {0}
        ORDER BY
            name asc
            """
                               .format(
                                   conditon
                               ), as_dict=1)

    if items_data:
        items = [d.item_code for d in items_data]
        item_prices_data = frappe.get_all("Item Price",
                                          fields=[
                                              "item_code", "price_list_rate", "currency"],
                                          filters={'price_list': price_list, 'item_code': ['in', items]})

        item_prices = {}
        for d in item_prices_data:
            item_prices[d.item_code] = d

        for item in items_data:
            item_code = item.item_code
            item_price = item_prices.get(item_code) or {}
            item_barcode = frappe.get_all("Item Barcode", filters={
                                          "parent": item_code}, fields=["barcode", "posa_uom"])

            if pos_profile.get("posa_display_items_in_stock"):
                item_stock_qty = get_stock_availability(
                    item_code, pos_profile.get("warehouse"))
            if pos_profile.get("posa_display_items_in_stock") and (not item_stock_qty or item_stock_qty < 0):
                pass
            else:
                row = {}
                row.update(item)
                row.update({
                    'rate': item_price.get('price_list_rate') or 0,
                    'currency': item_price.get('currency') or pos_profile.get("currency"),
                    'item_barcode': item_barcode or [],
                    'actual_qty': 0,
                })
                result.append(row)

    return result


def get_root_of(doctype):
    """Get root element of a DocType with a tree structure"""
    result = frappe.db.sql("""select t1.name from `tab{0}` t1 where
		(select count(*) from `tab{1}` t2 where
			t2.lft < t1.lft and t2.rgt > t1.rgt) = 0
		and t1.rgt > t1.lft""".format(doctype, doctype))
    return result[0][0] if result else None


@frappe.whitelist()
def get_items_groups():
    return frappe.db.sql("""
        select name 
        from `tabItem Group`
        where is_group = 0
        order by name
        LIMIT 0, 200 """, as_dict=1)


@frappe.whitelist()
def get_customer_names():
    customers = frappe.db.sql("""
        select name, mobile_no, email_id, tax_id
        from `tabCustomer`
        order by name
        LIMIT 0, 10000 """, as_dict=1)

    return customers


@frappe.whitelist()
def save_draft_invoice(data):
    data = json.loads(data)
    invoice_doc = frappe.get_doc(data)
    invoice_doc.flags.ignore_permissions = True
    frappe.flags.ignore_account_permission = True
    invoice_doc.set_missing_values()
    if invoice_doc.get("taxes"):
        for tax in invoice_doc.taxes:
            tax.included_in_print_rate = 1
    invoice_doc.save()
    return invoice_doc


@frappe.whitelist()
def update_invoice(data):
    data = json.loads(data)
    invoice_doc = frappe.get_doc("Sales Invoice", data.get("name"))
    invoice_doc.flags.ignore_permissions = True
    frappe.flags.ignore_account_permission = True
    invoice_doc.customer = data.get("customer")
    invoice_doc.items = []
    invoice_doc.discount_amount = data.get("discount_amount")
    invoice_doc.update({
        "items": data.get("items")
    })
    invoice_doc.set_missing_values()
    if invoice_doc.get("taxes"):
        for tax in invoice_doc.taxes:
            tax.included_in_print_rate = 1
    invoice_doc.save()
    return invoice_doc


@frappe.whitelist()
def submit_invoice(data):
    data = json.loads(data)
    invoice_doc = frappe.get_doc("Sales Invoice", data.get("name"))
    if data.get("loyalty_amount") > 0:
        invoice_doc.loyalty_amount = data.get("loyalty_amount")
        invoice_doc.redeem_loyalty_points = data.get("redeem_loyalty_points")
        invoice_doc.loyalty_points = data.get("loyalty_points")
    for payment in data.get("payments"):
        if payment.get("amount"):
            for i in invoice_doc.payments:
                if i.mode_of_payment == payment["mode_of_payment"]:
                    i.amount = payment["amount"]
                    break
    invoice_doc.flags.ignore_permissions = True
    frappe.flags.ignore_account_permission = True

    invoice_doc.save()
    if frappe.get_value("POS Profile", invoice_doc.pos_profile, "posa_allow_submissions_in_background_job"):
        enqueue(method=submit_in_background_job, queue='short',
                timeout=1000, is_async=True, kwargs=invoice_doc.name)
    else:
        invoice_doc.submit()
    return {
        "name": invoice_doc.name,
        "status": invoice_doc.docstatus
    }


def submit_in_background_job(kwargs):
    invoice_doc = frappe.get_doc("Sales Invoice", kwargs)
    invoice_doc.submit()


@frappe.whitelist()
def get_draft_invoices(pos_opening_shift):
    invoices_list = frappe.get_list(
        "Sales Invoice",
        filters={
            "posa_pos_opening_shift": pos_opening_shift,
            "docstatus": 0
        },
        fields=["name"],
        limit_page_length=0,
        order_by='customer'
    )
    data = []
    for invoice in invoices_list:
        data.append(frappe.get_doc("Sales Invoice", invoice["name"]))
    return data


@frappe.whitelist()
def delete_invoice(invoice):
    frappe.delete_doc("Sales Invoice", invoice, force=1)
    return "Inovice {0} Deleted".format(invoice)


@frappe.whitelist()
def get_items_details(pos_profile, items_data):
    pos_profile = json.loads(pos_profile)
    items_data = json.loads(items_data)
    warehouse = pos_profile.get("warehouse")
    result = []

    if len(items_data) > 0:
        for item in items_data:
            item_code = item.get("item_code")
            item_stock_qty = get_stock_availability(item_code, warehouse)
            has_batch_no, has_serial_no = frappe.get_value("Item", item_code, ["has_batch_no","has_serial_no"])

            uoms = frappe.get_all("UOM Conversion Detail", filters={
                "parent": item_code}, fields=["uom", "conversion_factor"])

            serial_no_data = frappe.get_all('Serial No', filters={
                "item_code": item_code,
                "status": "Active"
            }, fields=["name as serial_no"])

            if get_version() == 13: 
                batch_no_data = frappe.get_all('Batch',
                                            filters={
                                                "item": item_code,
                                                "batch_qty": [">", 0]
                                            },
                                            fields=["name as batch_no, batch_qty", "expiry_date"])
            elif get_version() == 12:
                batch_no_data = []
                from erpnext.stock.doctype.batch.batch import get_batch_qty
                batch_list = get_batch_qty(warehouse = warehouse, item_code = item_code)
                for batch in batch_list:
                    if batch.qty > 0 :
                        expiry_date = frappe.get_value("Batch",batch.batch_no, "expiry_date")
                        batch_no_data.append({
                            "batch_no": batch.batch_no,
                            "batch_qty": batch.qty,
                            "expiry_date": expiry_date
                        })
                    


            row = {}
            row.update(item)
            row.update({
                'item_uoms': uoms or [],
                'serial_no_data': serial_no_data or [],
                'batch_no_data': batch_no_data or [],
                'actual_qty': item_stock_qty or 0,
                'has_batch_no': has_batch_no,
                'has_serial_no': has_serial_no,
            })

            result.append(row)

    return result


@frappe.whitelist()
def get_item_detail(data, doc=None):
    return get_item_details(data, doc)


def get_stock_availability(item_code, warehouse):
    latest_sle = frappe.db.sql("""select sum(actual_qty) as  actual_qty
		from `tabStock Ledger Entry` 
		where item_code = %s and warehouse = %s
		limit 1""", (item_code, warehouse), as_dict=1)

    sle_qty = latest_sle[0].actual_qty or 0 if latest_sle else 0
    return sle_qty


@frappe.whitelist()
def create_customer(customer_name, tax_id, mobile_no, email_id):
    if not frappe.db.exists("Customer", {"customer_name": customer_name}):
        customer = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": customer_name,
            "tax_id": tax_id,
            "mobile_no": mobile_no,
            "email_id": email_id,
        }).insert(ignore_permissions=True)
        return customer


@frappe.whitelist()
def get_items_from_barcode(selling_price_list, currency, barcode):
    search_item = frappe.get_all("Item Barcode", filters={
        "barcode": barcode}, fields=["parent", "barcode", "posa_uom"])
    if len(search_item) == 0:
        return ""
    item_code = search_item[0].parent
    item_list = frappe.get_all("Item", filters={"name": item_code}, fields=[
        "name",
        "item_name",
        "description",
        "stock_uom",
        "image",
        "is_stock_item",
        "has_variants",
        "variant_of",
        "item_group",
        "has_batch_no",
        "has_serial_no"])

    if item_list[0]:
        item = item_list[0]
        item_prices_data = frappe.get_all("Item Price",
                                          fields=[
                                              "item_code", "price_list_rate", "currency"],
                                          filters={'price_list': selling_price_list, 'item_code': item_code})
        
        item_price = 0
        if len(item_prices_data):
            item_price = item_prices_data[0].get("price_list_rate")
            currency = item_prices_data[0].get("currency")
        
        item.update({
            'rate': item_price,
            'currency': currency,
            'item_code': item_code,
            'barcode' : barcode,
            'actual_qty': 0,
            'item_barcode': search_item,
        })
        return item


def get_version():
    from frappe.utils.gitutils import get_app_branch
    branch_name = get_app_branch("erpnext")
    if "12" in branch_name:
        return 12
    elif "13" in branch_name:
        return 13
    else:
        return 13