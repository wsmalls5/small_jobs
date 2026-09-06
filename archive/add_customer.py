# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
Small Jobs - Customer/Property Database Manager
Run this script to add, edit, view, or remove properties from customers.json.
One invoice is generated per property, so even if a customer owns multiple
properties (e.g. Teton Creek Retreat #1 and #2), each property gets its own entry.
"""

import csv
import json
import os
import re
import sys

CUSTOMERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'customers.json')


# --- File I/O -----------------------------------------------------------------

def load():
    if not os.path.exists(CUSTOMERS_FILE):
        return {}
    with open(CUSTOMERS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save(customers):
    with open(CUSTOMERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(customers, f, indent=2)
    print(f"\n  Saved -> {os.path.abspath(CUSTOMERS_FILE)}")


# --- Display helpers ----------------------------------------------------------

def clear():
    os.system('cls' if os.name == 'nt' else 'clear')

def rule():
    print("  " + "-" * 58)

def header(title):
    clear()
    print()
    print("  " + "=" * 60)
    print(f"  {title}")
    print("  " + "=" * 60)
    print()

def pause():
    input("\n  Press Enter to continue...")


# --- Input helpers ------------------------------------------------------------

def ask(label, default=None, required=False):
    """Prompt for input. Shows default in brackets; Enter keeps it."""
    hint = f" [{default}]" if default is not None else ""
    while True:
        val = input(f"  {label}{hint}: ").strip()
        if val:
            return val
        if default is not None:
            return default
        if not required:
            return ""
        print("  (required - please enter a value)")

def ask_float(label, default):
    while True:
        raw = ask(label, default=str(default))
        try:
            return float(raw)
        except ValueError:
            print("  Please enter a number (e.g. 70 or 0.10)")

def slugify(text):
    key = text.lower()
    key = re.sub(r'[^a-z0-9]+', '_', key)
    return key.strip('_')

def unique_key(base_key, existing_keys):
    key = base_key
    n = 2
    while key in existing_keys:
        key = f"{base_key}_{n}"
        n += 1
    return key


# --- Core actions -------------------------------------------------------------

def list_all(customers, pause_after=True):
    header("All Properties")
    if not customers:
        print("  No properties in database yet. Choose option 3 to add one.")
        if pause_after:
            pause()
        return

    print(f"  {'#':<4}  {'Property Label':<28}  {'Bill To':<22}  Email")
    rule()
    for i, (key, c) in enumerate(customers.items(), 1):
        label = c.get('property_label', '')[:27]
        bto   = c.get('bill_to_name', '')[:21]
        email = c.get('email', '')
        print(f"  {i:<4}  {label:<28}  {bto:<22}  {email}")
    rule()
    print(f"  {len(customers)} propert{'y' if len(customers) == 1 else 'ies'} total")

    if pause_after:
        pause()


def pick(customers, prompt_text="Select a property (0 to cancel)"):
    list_all(customers, pause_after=False)
    if not customers:
        return None, None
    keys = list(customers.keys())
    try:
        n = int(input(f"\n  {prompt_text}: ").strip())
        if n == 0:
            return None, None
        key = keys[n - 1]
        return key, customers[key]
    except (ValueError, IndexError):
        print("  Invalid selection.")
        return None, None


def view(customers):
    header("View Property Detail")
    key, data = pick(customers)
    if not key:
        return
    print()
    print(f"  Key (internal):   {key}")
    print(f"  Property label:   {data.get('property_label', '')}")
    print(f"  Bill to:          {data.get('bill_to_name', '')}")
    print(f"  Address:          {data.get('address', '')}")
    print(f"  Phone:            {data.get('phone', '')}")
    print(f"  Email:            {data.get('email', '')}")
    print(f"  Hourly rate:      ${data.get('hourly_rate', 70.0):.2f}/hr")
    aliases = data.get('aliases', [])
    print(f"  Name aliases:     {', '.join(aliases) if aliases else '(none)'}")
    pause()


def add(customers):
    header("Add New Property")
    print("  Fill in the details below. Press Enter to leave a field blank.\n")

    label = ask("Property label  (e.g. 'Murphy - Main' or 'Teton Creek #1')", required=True)
    key   = unique_key(slugify(label), customers)

    print()
    bill_to = ask("Bill-to name    (full name that appears on invoice)", required=True)
    address = ask("Property address")
    phone   = ask("Phone")
    email   = ask("Email")

    print()
    hourly_rate = ask_float("Hourly rate     ($/hr)", default=70.0)

    print("\n  Aliases - enter every name variation used in your hours/expenses")
    print("  files for this property (e.g. 'Murphy', 'murphy ', 'David Murphy').")
    print("  Blank line when done.\n")
    aliases = []
    while True:
        alias = input(f"  Alias {len(aliases) + 1}: ").strip()
        if not alias:
            break
        aliases.append(alias)

    customers[key] = {
        "property_label": label,
        "bill_to_name":   bill_to,
        "address":        address,
        "phone":          phone,
        "email":          email,
        "hourly_rate":    hourly_rate,
        "aliases":        aliases,
    }

    save(customers)
    print(f"\n  Added '{label}' with key '{key}'.")
    pause()


def edit(customers):
    header("Edit Property")
    key, data = pick(customers, prompt_text="Select property # to edit (0 to cancel)")
    if not key:
        return

    print(f"\n  Editing: {data['property_label']}")
    print("  Press Enter to keep the current value.\n")

    data['property_label'] = ask("Property label",  default=data.get('property_label', ''))
    data['bill_to_name']   = ask("Bill-to name",     default=data.get('bill_to_name', ''))
    data['address']        = ask("Address",          default=data.get('address', ''))
    data['phone']          = ask("Phone",            default=data.get('phone', ''))
    data['email']          = ask("Email",            default=data.get('email', ''))
    data['hourly_rate']    = ask_float("Hourly rate ($/hr)", default=data.get('hourly_rate', 70.0))

    aliases = data.get('aliases', [])
    while True:
        print(f"\n  Current aliases: {aliases if aliases else '(none)'}")
        print("  (a) Add alias   (r) Remove alias   (Enter) Done")
        action = input("  > ").strip().lower()
        if action == 'a':
            new_alias = input("  New alias: ").strip()
            if new_alias and new_alias not in aliases:
                aliases.append(new_alias)
                print(f"  Added: {new_alias}")
        elif action == 'r':
            if not aliases:
                print("  No aliases to remove.")
                continue
            for i, a in enumerate(aliases, 1):
                print(f"    {i}. {a}")
            try:
                n = int(input("  Remove number: ").strip())
                removed = aliases.pop(n - 1)
                print(f"  Removed: {removed}")
            except (ValueError, IndexError):
                print("  Invalid selection.")
        else:
            break

    data['aliases'] = aliases
    customers[key]  = data
    save(customers)
    print(f"\n  Updated '{data['property_label']}'.")
    pause()


def remove(customers):
    header("Remove Property")
    key, data = pick(customers, prompt_text="Select property to remove (0 to cancel)")
    if not key:
        return

    print(f"\n  You are about to permanently delete:")
    print(f"    {data['property_label']}  ({data.get('bill_to_name', '')})")
    confirm = input("\n  Type YES to confirm: ").strip()
    if confirm == "YES":
        del customers[key]
        save(customers)
        print("  Removed.")
    else:
        print("  Cancelled.")
    pause()


def export_csv(customers):
    header("Export to CSV")
    default_path = os.path.join(os.path.dirname(os.path.abspath(CUSTOMERS_FILE)), 'customers_export.csv')
    dest = ask("Save CSV to", default=default_path)
    with open(dest, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['#', 'Key', 'Property Label', 'Bill To', 'Address', 'Phone', 'Email', 'Rate (per hr)', 'Markup'])
        for i, (key, c) in enumerate(customers.items(), 1):
            w.writerow([
                i, key,
                c.get('property_label', ''),
                c.get('bill_to_name', ''),
                c.get('address', ''),
                c.get('phone', ''),
                c.get('email', ''),
                c.get('hourly_rate', ''),
                str(int(c.get('markup_rate', 0.1) * 100)) + '%',
            ])
    print(f"\n  Exported {len(customers)} properties -> {dest}")
    pause()


# --- Main menu ----------------------------------------------------------------

def main():
    while True:
        customers = load()
        count = len(customers)

        clear()
        print()
        print("  " + "=" * 60)
        print("  Small Jobs - Customer Database")
        print("  " + "=" * 60)
        print()
        print(f"  {count} propert{'y' if count == 1 else 'ies'} on file")
        print(f"  {os.path.abspath(CUSTOMERS_FILE)}")
        print()
        print("  1  List all properties")
        print("  2  View property detail")
        print("  3  Add new property")
        print("  4  Edit property")
        print("  5  Remove property")
        print("  6  Export to CSV")
        print("  7  Exit")
        print()

        choice = input("  > ").strip()

        if   choice == '1': list_all(customers)
        elif choice == '2': view(customers)
        elif choice == '3': add(customers)
        elif choice == '4': edit(customers)
        elif choice == '5': remove(customers)
        elif choice == '6': export_csv(customers)
        elif choice == '7':
            print("\n  Done.\n")
            sys.exit(0)
        else:
            print("  Invalid - enter 1-7.")
            pause()


if __name__ == '__main__':
    main()
