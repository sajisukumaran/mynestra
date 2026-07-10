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
    ("1400", "Property & Vehicles", ASSET, "1000", False, ""),
    ("1410", "Real Estate", ASSET, "1400", True, ""),
    ("1420", "Vehicles", ASSET, "1400", True, ""),
    ("1900", "Other Assets", ASSET, "1000", True, ""),
    ("1990", "Suspense", ASSET, "1000", True, "suspense"),
    # --- Liabilities ---
    ("2000", "Liabilities", LIABILITY, None, False, ""),
    # 2100 is a group header: the Cards module nests one postable sub-account per real credit card
    # beneath it (normal_side=credit), so per-card balances owed roll up here.
    ("2100", "Credit Cards", LIABILITY, "2000", False, "credit_cards"),
    ("2200", "Loans", LIABILITY, "2000", False, ""),
    ("2210", "Mortgage", LIABILITY, "2200", True, ""),
    ("2220", "Auto Loan", LIABILITY, "2200", True, ""),
    ("2230", "Personal Loan", LIABILITY, "2200", True, ""),
    ("2300", "Bills Payable", LIABILITY, "2000", True, ""),
    ("2400", "Taxes Payable", LIABILITY, "2000", True, "taxes_payable"),
    # --- Equity ---
    ("3000", "Equity", EQUITY, None, False, ""),
    ("3100", "Opening Balance Equity", EQUITY, "3000", True, "opening_balance_equity"),
    ("3200", "Net Worth", EQUITY, "3000", True, "net_worth"),
    ("3900", "Current Year Earnings", EQUITY, "3000", True, "current_year_earnings"),
    # --- Revenue / Income ---
    ("4000", "Revenue", REVENUE, None, False, ""),
    ("4100", "Salary & Wages", REVENUE, "4000", True, ""),
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
    ("4950", "Foreign Exchange Gain/Loss", REVENUE, "4000", True, "fx_gain_loss"),
    # --- Expenses ---
    ("5000", "Expenses", EXPENSE, None, False, ""),
    ("5100", "Housing", EXPENSE, "5000", False, ""),
    ("5110", "Rent / Mortgage Interest", EXPENSE, "5100", True, ""),
    ("5120", "Utilities", EXPENSE, "5100", True, ""),
    ("5130", "Maintenance", EXPENSE, "5100", True, ""),
    ("5200", "Food & Groceries", EXPENSE, "5000", True, ""),
    ("5300", "Transportation", EXPENSE, "5000", False, ""),
    ("5310", "Fuel", EXPENSE, "5300", True, ""),
    ("5320", "Vehicle Service", EXPENSE, "5300", True, ""),
    ("5330", "Transit", EXPENSE, "5300", True, ""),
    ("5400", "Health & Medical", EXPENSE, "5000", True, ""),
    ("5500", "Insurance", EXPENSE, "5000", True, ""),
    ("5600", "Education", EXPENSE, "5000", True, ""),
    ("5700", "Personal & Lifestyle", EXPENSE, "5000", True, ""),
    ("5800", "Taxes", EXPENSE, "5000", True, ""),
    ("5850", "Bank Charges", EXPENSE, "5000", True, "bank_charges"),
    ("5860", "Interest Expense", EXPENSE, "5000", True, "interest_expense"),
    # Advisory / account fees are expensed here; buy/sell commissions are capitalized into cost basis.
    ("5870", "Investment Fees", EXPENSE, "5000", True, "investment_fees"),
    ("5900", "Other Expenses", EXPENSE, "5000", True, ""),
]
