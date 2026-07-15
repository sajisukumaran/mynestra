"""Seed catalogs for the finance backbone — pure data, no Django imports (consumed by seed.py).

`CURRENCIES` is a curated ISO-4217 set (USD first — the default base currency; JPY exercises the
zero-decimal path). `CHART_OF_ACCOUNTS` is a sensible household chart with all five accounting
elements (Assets/Liabilities/Equity/Revenue/Expense), ordered parent-first so parents resolve in one
pass. Header rows (`is_postable=False`) roll up their children; only leaf accounts take postings.
`system_key`s are stable role handles the service resolves special accounts by.
"""

# (code, name, symbol, decimal_places)
CURRENCIES = [
    ("USD", "US Dollar", "$", 2),
    ("EUR", "Euro", "€", 2),
    ("GBP", "British Pound", "£", 2),
    ("INR", "Indian Rupee", "₹", 2),
    ("CAD", "Canadian Dollar", "$", 2),
    ("AUD", "Australian Dollar", "$", 2),
    ("JPY", "Japanese Yen", "¥", 0),
    ("CHF", "Swiss Franc", "Fr", 2),
    ("CNY", "Chinese Yuan", "¥", 2),
    ("SGD", "Singapore Dollar", "$", 2),
    ("AED", "UAE Dirham", "د.إ", 2),
    ("HKD", "Hong Kong Dollar", "$", 2),
    ("NZD", "New Zealand Dollar", "$", 2),
    ("ZAR", "South African Rand", "R", 2),
    ("SEK", "Swedish Krona", "kr", 2),
    ("SAR", "Saudi Riyal", "﷼", 2),
]

ASSET, LIABILITY, EQUITY, REVENUE, EXPENSE = (
    "ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE",
)

# (code, name, type, parent_code, is_postable, system_key)
CHART_OF_ACCOUNTS = [
    # --- Assets ---
    ("1000", "Assets", ASSET, None, False, ""),
    ("1100", "Cash & Bank", ASSET, "1000", False, ""),
    ("1110", "Cash on Hand", ASSET, "1100", True, ""),
    # 1120/1130 are group headers: the Banking module nests one postable sub-account per real
    # bank account beneath them, so per-account balances roll up here (never posted to directly).
    ("1120", "Checking Account", ASSET, "1100", False, ""),
    ("1130", "Savings Account", ASSET, "1100", False, ""),
    # 1140 is a group header too: the Banking module nests one postable sub-account per bank CD
    # (certificate of deposit / term deposit) beneath it, so per-CD balances roll up here.
    ("1140", "Certificates of Deposit", ASSET, "1100", False, "certificates_of_deposit"),
    ("1150", "Inter-account Transfer", ASSET, "1100", True, "transfer_clearing"),
    # 1200 groups all investment holdings; 1210/1220/1230 are group headers (like 1120/1130 for
    # banking): the Investments module nests one postable sub-account per real investment account
    # beneath the header matching its tax registration, so per-account balances roll up here.
    ("1200", "Investments", ASSET, "1000", False, "investments"),
    ("1210", "Brokerage", ASSET, "1200", False, "brokerage"),
    ("1220", "Retirement", ASSET, "1200", False, "retirement"),
    ("1230", "HSA", ASSET, "1200", False, "hsa"),
    ("1300", "Receivables", ASSET, "1000", True, ""),
    ("1310", "Prepaid Expenses", ASSET, "1000", True, ""),
    # Refundable deposits held as an asset (e.g. a vehicle lease security deposit): recovered on
    # lease return, or applied against a buyout. The Automobile module posts lease deposits here.
    ("1320", "Refundable Deposits", ASSET, "1000", True, "refundable_deposits"),
    ("1400", "Property & Vehicles", ASSET, "1000", False, ""),
    ("1410", "Real Estate", ASSET, "1400", True, ""),
    # 1420 is a group header: the Automobile module nests one postable sub-account per owned vehicle
    # beneath it (held at cost), so per-vehicle basis rolls up here (never posted to directly).
    ("1420", "Vehicles", ASSET, "1400", False, "vehicles"),
    # Capitalized durable household goods (electronics/appliances/furniture): the Payables module
    # nests warranty-tracked purchases here instead of expensing them.
    ("1430", "Household Goods & Equipment", ASSET, "1400", True, "household_goods"),
    ("1900", "Other Assets", ASSET, "1000", True, ""),
    ("1990", "Suspense", ASSET, "1000", True, "suspense"),
    # --- Liabilities ---
    ("2000", "Liabilities", LIABILITY, None, False, ""),
    # 2100 is a group header: the Cards module nests one postable sub-account per real credit card
    # beneath it (normal_side=credit), so per-card balances owed roll up here.
    ("2100", "Credit Cards", LIABILITY, "2000", False, "credit_cards"),
    # 2200 groups all loans; 2210..2260 are per-type group headers (like 1120/1130 for banking): the
    # Loans module nests one postable sub-account per loan under the header matching its loan_type,
    # so per-loan balances roll up by type here (never posted to). Future Auto/Home modules resolve
    # a type by system_key (e.g. "loans_auto") or by Loan.loan_type.
    ("2200", "Loans", LIABILITY, "2000", False, "loans"),
    ("2210", "Mortgage", LIABILITY, "2200", False, "loans_mortgage"),
    ("2220", "Auto Loan", LIABILITY, "2200", False, "loans_auto"),
    ("2230", "Personal Loan", LIABILITY, "2200", False, "loans_personal"),
    ("2240", "Student Loan", LIABILITY, "2200", False, "loans_student"),
    ("2250", "HELOC", LIABILITY, "2200", False, "loans_heloc"),
    ("2260", "Line of Credit", LIABILITY, "2200", False, "loans_line_of_credit"),
    # 2300 is the Accounts-Payable control account: vendor bills credit it (accrual), payments
    # debit it. Per-vendor aging uses the line-level party dimension, not sub-accounts.
    ("2300", "Accounts Payable", LIABILITY, "2000", True, "accounts_payable"),
    ("2400", "Taxes Payable", LIABILITY, "2000", True, "taxes_payable"),
    # 2900 groups generic non-loan liabilities (tax-payment plans, private notes). 2950 holds
    # contingent / co-signed liabilities that are tracked but EXCLUDED from net worth (off-balance-
    # sheet — see services.net_worth). The Loans module nests a postable sub-account per liability
    # under each, matching the loan's counts_toward_net_worth flag.
    ("2900", "Other Liabilities", LIABILITY, "2000", False, "other_liabilities"),
    ("2950", "Contingent Liabilities", LIABILITY, "2000", False, "contingent_liabilities"),
    # --- Equity ---
    ("3000", "Equity", EQUITY, None, False, ""),
    ("3100", "Opening Balance Equity", EQUITY, "3000", True, "opening_balance_equity"),
    ("3200", "Net Worth", EQUITY, "3000", True, "net_worth"),
    ("3900", "Current Year Earnings", EQUITY, "3000", True, "current_year_earnings"),
    # --- Revenue / Income ---
    ("4000", "Revenue", REVENUE, None, False, ""),
    ("4100", "Salary & Wages", REVENUE, "4000", True, ""),
    # Employer retirement-plan match / contributions (categorize a match contribution here so it
    # reports as compensation income distinct from salary).
    ("4150", "Employer Match", REVENUE, "4000", True, "employer_match"),
    ("4200", "Business Income", REVENUE, "4000", True, ""),
    # 4300 is a group header for investment income; the Investments module posts dividends, realized
    # gains, capital-gains distributions and investment interest to its postable children below.
    ("4300", "Investment Income", REVENUE, "4000", False, ""),
    ("4310", "Dividend Income", REVENUE, "4300", True, "dividend_income"),
    ("4320", "Realized Capital Gain/Loss", REVENUE, "4300", True, "realized_capital_gain"),
    ("4330", "Capital Gains Distributions", REVENUE, "4300", True, "capital_gains_distribution"),
    ("4340", "Investment Interest", REVENUE, "4300", True, "investment_interest"),
    # 4400 stays a standalone postable account (Banking posts bank interest here by code).
    ("4400", "Interest & Dividends", REVENUE, "4000", True, ""),
    ("4900", "Other Income", REVENUE, "4000", True, ""),
    # Early-payment / purchase discounts taken on vendor bills (reduces the net cost of purchases).
    ("4920", "Purchase Discounts", REVENUE, "4000", True, "purchase_discounts"),
    # Gain/loss on disposing a capitalized asset (a vehicle sale/trade-in/total-loss); REVENUE-typed
    # so it can run negative (a loss). Mirrors 4320 Realized Capital Gain/Loss for investments.
    ("4930", "Gain/Loss on Asset Sale", REVENUE, "4000", True, "asset_disposal_gain_loss"),
    ("4950", "Foreign Exchange Gain/Loss", REVENUE, "4000", True, "fx_gain_loss"),
    # --- Expenses ---
    ("5000", "Expenses", EXPENSE, None, False, ""),
    ("5100", "Housing", EXPENSE, "5000", False, ""),
    ("5110", "Rent / Mortgage Interest", EXPENSE, "5100", True, ""),
    ("5120", "Utilities", EXPENSE, "5100", True, ""),
    ("5130", "Maintenance", EXPENSE, "5100", True, ""),
    # Escrow components on a mortgage payment default here (property tax) and to 5150 (insurance);
    # both remappable per-loan in Expert mode.
    ("5140", "Property Tax", EXPENSE, "5100", True, "property_tax"),
    ("5150", "Home Insurance", EXPENSE, "5100", True, "home_insurance"),
    ("5200", "Food & Groceries", EXPENSE, "5000", True, ""),
    ("5300", "Transportation", EXPENSE, "5000", False, ""),
    ("5310", "Fuel", EXPENSE, "5300", True, ""),
    ("5320", "Vehicle Service", EXPENSE, "5300", True, ""),
    ("5330", "Transit", EXPENSE, "5300", True, ""),
    # Automobile module running-cost homes (auto insurance premiums, registration / road tax, and
    # lease payments — leases are off balance sheet, so a lease payment is an expense here).
    ("5340", "Vehicle Insurance", EXPENSE, "5300", True, "vehicle_insurance"),
    ("5350", "Vehicle Registration", EXPENSE, "5300", True, "vehicle_registration"),
    ("5360", "Vehicle Lease", EXPENSE, "5300", True, "vehicle_lease"),
    ("5400", "Health & Medical", EXPENSE, "5000", True, ""),
    ("5500", "Insurance", EXPENSE, "5000", True, ""),
    ("5600", "Education", EXPENSE, "5000", True, ""),
    ("5700", "Personal & Lifestyle", EXPENSE, "5000", True, ""),
    ("5800", "Taxes", EXPENSE, "5000", True, ""),
    ("5850", "Bank Charges", EXPENSE, "5000", True, "bank_charges"),
    ("5860", "Interest Expense", EXPENSE, "5000", True, "interest_expense"),
    # Advisory / account fees are expensed here; buy/sell commissions are capitalized into basis.
    ("5870", "Investment Fees", EXPENSE, "5000", True, "investment_fees"),
    # Payments-in-lieu of dividends to a share lender while holding a short position (distinct from
    # interest expense and advisory fees).
    ("5880", "Substitute Dividend Expense", EXPENSE, "5000", True, "substitute_dividend_expense"),
    ("5900", "Other Expenses", EXPENSE, "5000", True, ""),
    # Payables: shipping/freight on purchases, and non-recoverable sales tax paid on purchases
    # (defaults for explicit Shipping / Tax bill lines; line-level tax otherwise folds into cost).
    ("5920", "Shipping & Delivery", EXPENSE, "5000", True, "shipping_expense"),
    ("5930", "Sales Tax", EXPENSE, "5000", True, "sales_tax_paid"),
]
