"""sales_audit_schema.py — the questions the in-person Cavnar AI audit asks.

One definition, served to the browser and read by the engine, so the form,
the calculations and the report can never disagree about what a field
means. Every question has a stable id (never renamed once audits exist —
answers are stored by id), a type, and optional conversational phrasing
("say" — how to ask it out loud) and "show_if" conditions for follow-ups.

Types: currency, percent, integer, decimal, yesno, choice, multi, rating,
text, textarea, date, tools (the repeated software-cost group).
"""

SECTIONS = [
    {"key": "profile", "label": "Restaurant Profile", "short": "Profile",
     "intro": "Who they are and what they run on.",
     "questions": [
        {"id": "restaurant_name", "label": "Restaurant name", "type": "text", "required": True},
        {"id": "owner_name", "label": "Owner", "type": "text", "required": True},
        {"id": "owner_email", "label": "Owner email", "type": "text"},
        {"id": "owner_phone", "label": "Owner phone", "type": "text"},
        {"id": "address", "label": "Address", "type": "text"},
        {"id": "city", "label": "City", "type": "text"},
        {"id": "state", "label": "State", "type": "text"},
        {"id": "locations", "label": "Number of locations", "type": "integer", "min": 1},
        {"id": "restaurant_type", "label": "Restaurant type", "type": "choice",
         "options": ["Upscale sports bar", "Sports bar", "Bar / pub", "Casual full-service", "Upscale full-service",
                     "Fine dining", "Fast casual", "Quick service", "Café / bakery", "Pizzeria", "Other"]},
        {"id": "service_model", "label": "Service model", "type": "choice",
         "options": ["Full-service", "Counter service", "Hybrid", "Bar-forward"]},
        {"id": "years_in_business", "label": "Years in business", "type": "decimal", "min": 0},
        {"id": "seating_capacity", "label": "Seating capacity", "type": "integer", "min": 0},
        {"id": "hours", "label": "Hours of operation", "type": "text", "placeholder": "e.g. 11am–1am Sun–Thu, 11am–2am Fri–Sat"},
        {"id": "days_open", "label": "Days open per week", "type": "integer", "min": 1, "max": 7},
        {"id": "pos_system", "label": "POS system", "type": "choice", "allow_other": True,
         "options": ["Toast", "Square", "Clover", "Aloha", "Micros / Oracle", "Lightspeed", "SpotOn", "TouchBistro", "Other"]},
        {"id": "payroll_system", "label": "Payroll system", "type": "text"},
        {"id": "accounting_system", "label": "Accounting system", "type": "text"},
        {"id": "reservation_system", "label": "Reservation / waitlist system", "type": "text"},
        {"id": "marketing_tools", "label": "Marketing tools", "type": "text"},
        {"id": "review_tools", "label": "Review / reputation tools", "type": "text"},
        {"id": "inventory_system", "label": "Inventory system", "type": "text"},
     ]},

    {"key": "financial", "label": "Financial Snapshot", "short": "Financials",
     "intro": "Collect whatever they're comfortable sharing. Anything missing that can be derived is calculated and labelled.",
     "questions": [
        {"id": "fin_annual_revenue", "label": "Annual revenue", "type": "currency",
         "say": "Roughly what did the restaurant do in sales last year?"},
        {"id": "fin_monthly_revenue", "label": "Monthly revenue", "type": "currency", "derive": "annual/12"},
        {"id": "fin_weekly_revenue", "label": "Weekly revenue", "type": "currency", "derive": "annual/52"},
        {"id": "fin_daily_sales", "label": "Average daily sales", "type": "currency"},
        {"id": "fin_avg_check", "label": "Average check (per guest)", "type": "currency",
         "say": "What does the average guest spend, food and drink together?"},
        {"id": "fin_covers_week", "label": "Covers / guests per week", "type": "integer", "min": 0},
        {"id": "fin_transactions_week", "label": "Transactions per week", "type": "integer", "min": 0},
        {"id": "fin_gross_profit", "label": "Gross profit (annual)", "type": "currency"},
        {"id": "fin_net_profit", "label": "Net profit (annual)", "type": "currency", "allow_negative": True},
        {"id": "fin_net_margin", "label": "Net margin", "type": "percent", "allow_negative": True, "derive": "net profit / revenue"},
        {"id": "fin_prime_cost_pct", "label": "Prime cost (labor + COGS)", "type": "percent", "derive": "labor % + food % + beverage %"},
        {"id": "fin_labor_cost", "label": "Labor cost (annual $)", "type": "currency"},
        {"id": "fin_food_cost", "label": "Food cost (annual $)", "type": "currency"},
        {"id": "fin_bev_cost", "label": "Beverage cost (annual $)", "type": "currency"},
        {"id": "fin_rent", "label": "Rent (monthly)", "type": "currency"},
        {"id": "fin_utilities", "label": "Utilities (monthly)", "type": "currency"},
        {"id": "fin_marketing_spend", "label": "Marketing spend (monthly)", "type": "currency"},
        {"id": "fin_software_spend", "label": "Software spend (monthly)", "type": "currency"},
        {"id": "fin_processing_fees", "label": "Merchant processing fees (monthly)", "type": "currency"},
        {"id": "fin_other_expenses", "label": "Other major expenses", "type": "textarea"},
     ]},

    {"key": "labor", "label": "Labor", "short": "Labor",
     "intro": "Where the biggest controllable dollars usually are. Get the % first, then how they manage it.",
     "questions": [
        {"id": "lab_labor_pct", "label": "Labor % of sales", "type": "percent",
         "say": "Do you know roughly where labor has been running as a percentage of sales?"},
        {"id": "lab_labor_pct_basis", "label": "That number is…", "type": "choice",
         "options": ["Wages only", "Fully loaded (taxes + benefits)", "Not sure"], "show_if": {"lab_labor_pct": "*"}},
        {"id": "lab_labor_dollars", "label": "Total labor dollars", "type": "currency"},
        {"id": "lab_labor_period", "label": "…per", "type": "choice", "options": ["Week", "Month", "Year"],
         "show_if": {"lab_labor_dollars": "*"}},
        {"id": "lab_target_pct", "label": "Labor target %", "type": "percent",
         "say": "Do you have a labor number you try to hit?"},
        {"id": "lab_employees", "label": "Number of employees", "type": "integer", "min": 0},
        {"id": "lab_hourly", "label": "Hourly employees", "type": "integer", "min": 0},
        {"id": "lab_salaried", "label": "Salaried employees", "type": "integer", "min": 0},
        {"id": "lab_mgmt_payroll", "label": "Management payroll (annual)", "type": "currency"},
        {"id": "lab_avg_wage", "label": "Average hourly wage", "type": "currency"},
        {"id": "lab_overtime", "label": "Do you experience overtime regularly?", "type": "yesno"},
        {"id": "lab_ot_hours_week", "label": "Overtime hours per week", "type": "decimal", "min": 0, "show_if": {"lab_overtime": "yes"}},
        {"id": "lab_ot_dollars_week", "label": "Estimated overtime cost per week", "type": "currency", "show_if": {"lab_overtime": "yes"}},
        {"id": "lab_ot_positions", "label": "Which positions?", "type": "text", "show_if": {"lab_overtime": "yes"}},
        {"id": "lab_ot_why", "label": "Why does it happen?", "type": "textarea", "show_if": {"lab_overtime": "yes"}},
        {"id": "lab_ot_surprise", "label": "How often does overtime surprise you at payroll?", "type": "choice",
         "options": ["Never", "Occasionally", "Most pay periods", "Every pay period"]},
        {"id": "lab_schedule_how", "label": "How are schedules built?", "type": "choice",
         "options": ["Manager intuition", "Copy last week", "Based on forecasted sales", "Software forecast", "Mix"]},
        {"id": "lab_schedule_software", "label": "Scheduling software", "type": "text"},
        {"id": "lab_schedule_accuracy", "label": "Schedule accuracy vs. actual need", "type": "rating",
         "say": "On a 1–5, how often does the schedule match what the shift actually needed?"},
        {"id": "lab_understaffed", "label": "Understaffing happens…", "type": "choice",
         "options": ["Rarely", "Some shifts", "Weekly", "Most days"]},
        {"id": "lab_overstaffed", "label": "Overstaffing happens…", "type": "choice",
         "options": ["Rarely", "Some shifts", "Weekly", "Most days"]},
        {"id": "lab_calloffs", "label": "Call-offs per week", "type": "decimal", "min": 0},
        {"id": "lab_turnover", "label": "Annual turnover (approx %)", "type": "percent", "max": 300},
        {"id": "lab_training", "label": "Training process", "type": "choice", "options": ["Structured", "Informal", "None"]},
        {"id": "lab_timeclock", "label": "Time-clock issues (early clock-ins, late clock-outs)?", "type": "yesno"},
        {"id": "lab_timeclock_detail", "label": "What happens?", "type": "text", "show_if": {"lab_timeclock": "yes"}},
        {"id": "lab_breaks", "label": "Break compliance tracked?", "type": "yesno"},
        {"id": "lab_sales_forecast", "label": "Do you forecast sales by day?", "type": "yesno"},
        {"id": "lab_labor_forecast", "label": "Do you forecast labor need from that?", "type": "yesno"},
        {"id": "lab_know_daily", "label": "Do you know your labor % every day?", "type": "yesno"},
        {"id": "lab_how_fast", "label": "How quickly do you know labor is running high?", "type": "choice",
         "options": ["Same shift", "Next day", "End of week", "At payroll", "End of month or later"]},
        {"id": "lab_by_day", "label": "Do you see labor by day?", "type": "yesno"},
        {"id": "lab_by_shift", "label": "By shift?", "type": "yesno"},
        {"id": "lab_by_role", "label": "By role?", "type": "yesno"},
        {"id": "lab_manager_adjust", "label": "Do managers cut or add staff during a shift?", "type": "choice",
         "options": ["Always", "Sometimes", "Rarely", "Never"]},
        {"id": "lab_owner_checks", "label": "How often do you personally check labor?", "type": "choice",
         "options": ["Daily", "Weekly", "At payroll", "Monthly", "Rarely"]},
        {"id": "lab_waste_where", "label": "Where do you feel you're wasting the most labor?", "type": "textarea"},
        {"id": "lab_hard_shifts", "label": "Which shifts are hardest to staff correctly?", "type": "text"},
        {"id": "lab_frustration", "label": "What labor problem frustrates you most?", "type": "textarea"},
     ]},

    {"key": "food", "label": "Food Cost", "short": "Food Cost",
     "intro": "Actual vs. theoretical is the whole conversation. Find out what they know and how often they look.",
     "questions": [
        {"id": "food_cost_pct", "label": "Food cost %", "type": "percent",
         "say": "Do you know roughly where your food cost has been running lately?"},
        {"id": "food_cost_basis", "label": "That number is…", "type": "choice",
         "options": ["Actual (from inventory)", "Theoretical (from recipes)", "Purchases ÷ sales", "Not sure"],
         "show_if": {"food_cost_pct": "*"}},
        {"id": "food_includes_bev", "label": "Is that food only, or food + beverage combined?", "type": "choice",
         "options": ["Food only", "Combined", "Not sure"], "show_if": {"food_cost_pct": "*"}},
        {"id": "food_target_pct", "label": "Food cost target %", "type": "percent"},
        {"id": "food_sales", "label": "Food sales (annual)", "type": "currency"},
        {"id": "food_purchases", "label": "Food purchases (annual)", "type": "currency"},
        {"id": "food_cogs", "label": "Total COGS (annual)", "type": "currency"},
        {"id": "food_inventory_freq", "label": "How often do you take inventory?", "type": "choice",
         "options": ["Weekly", "Every two weeks", "Monthly", "Quarterly", "Rarely / never"]},
        {"id": "food_begin_inv", "label": "Beginning inventory (last period)", "type": "currency"},
        {"id": "food_end_inv", "label": "Ending inventory (last period)", "type": "currency"},
        {"id": "food_waste_week", "label": "Estimated waste + spoilage per week", "type": "currency",
         "say": "If you had to guess, how much food goes in the bin each week?"},
        {"id": "food_waste_tracked", "label": "Is waste logged?", "type": "yesno"},
        {"id": "food_comps_week", "label": "Comps per week", "type": "currency"},
        {"id": "food_discounts_week", "label": "Discounts per week", "type": "currency"},
        {"id": "food_employee_meals", "label": "Employee meals per week", "type": "currency"},
        {"id": "food_theft", "label": "Theft concerns?", "type": "choice", "options": ["None", "Some", "Significant"]},
        {"id": "food_portion", "label": "Portion consistency", "type": "rating",
         "say": "1–5, how consistent are portions from cook to cook?"},
        {"id": "food_recipes_costed", "label": "Are recipes costed?", "type": "choice",
         "options": ["All, kept current", "Most", "Some", "None"]},
        {"id": "food_theoretical", "label": "Do you know theoretical vs. actual food cost?", "type": "yesno"},
        {"id": "food_theoretical_gap", "label": "Gap between them (points)", "type": "decimal", "min": 0, "show_if": {"food_theoretical": "yes"}},
        {"id": "food_vendor_notice", "label": "How quickly do you notice vendor price increases?", "type": "choice",
         "options": ["Same invoice", "Within the week", "Month-end", "When margins drop", "Rarely notice"]},
        {"id": "food_vendor_compare", "label": "Do you compare prices between vendors?", "type": "choice",
         "options": ["Regularly", "Occasionally", "Never"]},
        {"id": "food_invoice_review", "label": "Who reviews invoices?", "type": "text"},
        {"id": "food_menu_pricing_freq", "label": "How often is menu pricing reviewed?", "type": "choice",
         "options": ["Monthly", "Quarterly", "Yearly", "When costs bite", "Rarely"]},
        {"id": "food_item_margins", "label": "Do you know which items have the best and worst margins?", "type": "yesno"},
        {"id": "food_high_cost_items", "label": "High-cost items", "type": "text"},
        {"id": "food_low_margin_items", "label": "Low-margin items", "type": "text"},
        {"id": "food_purchasing", "label": "Purchasing process", "type": "textarea"},
        {"id": "food_alerts", "label": "Do managers get food-cost alerts before month-end?", "type": "yesno"},
        {"id": "food_confidence", "label": "Confidence in current food cost number", "type": "rating"},
        {"id": "food_waste_where", "label": "Where does most waste happen?", "type": "textarea"},
     ]},

    {"key": "bar", "label": "Bar & Alcohol", "short": "Bar",
     "intro": "For a bar-forward concept this is often the biggest hidden number. Pour cost, variance, comps.",
     "upcoming_module": True,
     "questions": [
        {"id": "bar_alcohol_sales", "label": "Alcohol sales (annual)", "type": "currency"},
        {"id": "bar_alcohol_pct", "label": "Alcohol % of total sales", "type": "percent",
         "say": "Roughly what share of sales is the bar?"},
        {"id": "bar_beer_sales", "label": "Beer sales (annual)", "type": "currency"},
        {"id": "bar_wine_sales", "label": "Wine sales (annual)", "type": "currency"},
        {"id": "bar_liquor_sales", "label": "Liquor sales (annual)", "type": "currency"},
        {"id": "bar_bev_cost_pct", "label": "Overall beverage (pour) cost %", "type": "percent",
         "say": "Do you know your true pour cost across the bar?"},
        {"id": "bar_liquor_cost_pct", "label": "Liquor cost %", "type": "percent"},
        {"id": "bar_beer_cost_pct", "label": "Beer cost %", "type": "percent"},
        {"id": "bar_wine_cost_pct", "label": "Wine cost %", "type": "percent"},
        {"id": "bar_target_pct", "label": "Pour cost target %", "type": "percent"},
        {"id": "bar_avg_drink", "label": "Average drink price", "type": "currency"},
        {"id": "bar_inventory_freq", "label": "How often is bar inventory taken?", "type": "choice",
         "options": ["Weekly", "Every two weeks", "Monthly", "Quarterly", "Rarely / never"]},
        {"id": "bar_theoretical", "label": "Do you measure theoretical vs. actual beverage cost?", "type": "yesno"},
        {"id": "bar_variance_pct", "label": "Inventory variance (% of usage unaccounted for)", "type": "percent",
         "say": "When you count, how much product is missing versus what the POS says you sold?"},
        {"id": "bar_pour_method", "label": "Pour control", "type": "choice",
         "options": ["Free pour", "Jiggers required", "Measured pourers", "Mix"]},
        {"id": "bar_overpour", "label": "How do you identify overpouring?", "type": "choice",
         "options": ["Don't", "Watching", "Variance reports", "Pour-spout tech"]},
        {"id": "bar_comps_week", "label": "Complimentary drinks per week ($)", "type": "currency"},
        {"id": "bar_comp_policy", "label": "Bartender comp policy", "type": "choice",
         "options": ["Tracked and limited", "Tracked", "Untracked", "No policy"]},
        {"id": "bar_spills", "label": "Spills and breakage tracked?", "type": "yesno"},
        {"id": "bar_theft", "label": "Theft concerns?", "type": "choice", "options": ["None", "Some", "Significant"]},
        {"id": "bar_recipes", "label": "Cocktail recipes costed and specced?", "type": "choice",
         "options": ["All", "Most", "Some", "None"]},
        {"id": "bar_recipe_consistency", "label": "Recipe consistency across bartenders", "type": "rating"},
        {"id": "bar_bottle_yield", "label": "Bottle yields tracked?", "type": "yesno"},
        {"id": "bar_draft_waste", "label": "Draft beer waste / keg variance", "type": "choice",
         "options": ["Tracked and low", "Tracked, a problem", "Not tracked"]},
        {"id": "bar_drink_margins", "label": "Do you know which drinks have the highest contribution margin?", "type": "yesno"},
        {"id": "bar_cocktail_profit", "label": "Do you analyze profitability by cocktail?", "type": "yesno"},
        {"id": "bar_happy_hour", "label": "Happy hour performance", "type": "choice",
         "options": ["Measured, profitable", "Measured, break-even", "Not measured", "No happy hour"]},
        {"id": "bar_vendor_pricing", "label": "How often do vendor price changes leave menu prices outdated?", "type": "choice",
         "options": ["Rarely", "Sometimes", "Often", "Constantly"]},
        {"id": "bar_disappear", "label": "Do you know how much product disappears through spills, comps and variance?", "type": "yesno"},
        {"id": "bar_notes_q", "label": "Anything else about the bar?", "type": "textarea"},
     ]},

    {"key": "reviews", "label": "Reviews", "short": "Reviews",
     "intro": "Reputation is revenue for an independent. Rating, volume, response habits, recurring themes.",
     "questions": [
        {"id": "rev_google_rating", "label": "Google rating", "type": "decimal", "min": 1, "max": 5},
        {"id": "rev_google_count", "label": "Google review count", "type": "integer", "min": 0},
        {"id": "rev_yelp_rating", "label": "Yelp rating", "type": "decimal", "min": 1, "max": 5},
        {"id": "rev_facebook_rating", "label": "Facebook rating", "type": "decimal", "min": 1, "max": 5},
        {"id": "rev_per_month", "label": "New reviews per month", "type": "integer", "min": 0},
        {"id": "rev_negative_month", "label": "Negative reviews per month (1–3★)", "type": "integer", "min": 0},
        {"id": "rev_response_rate", "label": "Response rate", "type": "percent",
         "say": "Roughly what share of reviews get a reply?"},
        {"id": "rev_response_time", "label": "Average response time", "type": "choice",
         "options": ["Same day", "1–2 days", "Within a week", "Longer", "Don't respond"]},
        {"id": "rev_who_responds", "label": "Who responds?", "type": "text"},
        {"id": "rev_personalized", "label": "Are responses personalized?", "type": "choice",
         "options": ["Always", "Mostly", "Templated", "N/A"]},
        {"id": "rev_read_freq", "label": "How often do you read reviews?", "type": "choice",
         "options": ["Daily", "Weekly", "Monthly", "When someone flags one"]},
        {"id": "rev_categorize", "label": "Do you categorize recurring complaints?", "type": "yesno"},
        {"id": "rev_top_complaints", "label": "Top 3 complaints right now", "type": "textarea",
         "say": "Can you tell me your top three customer complaints right now?"},
        {"id": "rev_top_compliments", "label": "Recurring compliments", "type": "textarea"},
        {"id": "rev_competitor_rating", "label": "Nearest competitor's rating", "type": "decimal", "min": 1, "max": 5},
        {"id": "rev_track_sentiment", "label": "Do you track sentiment trends over time?", "type": "yesno"},
        {"id": "rev_managers_see", "label": "Do managers see review trends?", "type": "yesno"},
        {"id": "rev_changed_ops", "label": "Ever changed operations because of a review pattern?", "type": "yesno"},
        {"id": "rev_unanswered", "label": "Do negative reviews sometimes go unanswered?", "type": "yesno"},
        {"id": "rev_know_drivers", "label": "Do you know which items, staff or ops issues drive poor sentiment?", "type": "yesno"},
     ]},

    {"key": "marketing", "label": "Marketing", "short": "Marketing",
     "intro": "Spend, channels, and whether anyone knows what actually brings people in.",
     "questions": [
        {"id": "mkt_spend_month", "label": "Total marketing spend (monthly)", "type": "currency"},
        {"id": "mkt_paid_ads", "label": "Paid advertising (monthly)", "type": "currency"},
        {"id": "mkt_social_spend", "label": "Social media spend (monthly)", "type": "currency"},
        {"id": "mkt_google_ads", "label": "Google Ads (monthly)", "type": "currency"},
        {"id": "mkt_meta_ads", "label": "Meta Ads (monthly)", "type": "currency"},
        {"id": "mkt_agency_cost", "label": "Agency cost (monthly)", "type": "currency"},
        {"id": "mkt_agency_replace", "label": "Would you replace the agency if content and campaigns were handled?", "type": "choice",
         "options": ["Yes", "Maybe", "No"], "show_if": {"mkt_agency_cost": "*"}},
        {"id": "mkt_software_cost", "label": "Marketing software (monthly)", "type": "currency"},
        {"id": "mkt_email", "label": "Email marketing?", "type": "yesno"},
        {"id": "mkt_sms", "label": "SMS marketing?", "type": "yesno"},
        {"id": "mkt_loyalty", "label": "Loyalty program?", "type": "yesno"},
        {"id": "mkt_promos", "label": "Promotions and discounts run", "type": "textarea"},
        {"id": "mkt_db_size", "label": "Customer database size", "type": "integer", "min": 0},
        {"id": "mkt_email_list", "label": "Email list size", "type": "integer", "min": 0},
        {"id": "mkt_sms_list", "label": "SMS list size", "type": "integer", "min": 0},
        {"id": "mkt_social_following", "label": "Social following (total)", "type": "integer", "min": 0},
        {"id": "mkt_campaign_freq", "label": "Campaign frequency", "type": "choice",
         "options": ["Weekly+", "Monthly", "Occasional", "Rarely"]},
        {"id": "mkt_roi_tracked", "label": "Is campaign ROI tracked?", "type": "yesno"},
        {"id": "mkt_know_channels", "label": "Do you know which channels actually produce revenue?", "type": "yesno"},
        {"id": "mkt_decide", "label": "How do you decide what promotions to run?", "type": "choice",
         "options": ["Sales data", "Gut instinct", "Whatever worked before", "Agency decides"]},
        {"id": "mkt_slow_days", "label": "Do you market differently on slow days?", "type": "yesno"},
        {"id": "mkt_lapsed", "label": "Do you know which customers haven't returned recently?", "type": "yesno"},
        {"id": "mkt_retention_auto", "label": "Automated retention campaigns?", "type": "yesno"},
        {"id": "mkt_promo_when_busy", "label": "Are promotions ever sent when you don't need demand?", "type": "yesno"},
        {"id": "mkt_profit_after_discount", "label": "Do you measure profit after discounts, not just sales?", "type": "yesno"},
        {"id": "mkt_hours_week", "label": "Hours per week spent on marketing", "type": "decimal", "min": 0},
     ]},

    {"key": "waitlist", "label": "Waitlist & Guest Flow", "short": "Guest Flow",
     "intro": "Walkaways are lost covers. Quoted vs. actual waits, turns, no-shows.",
     "upcoming_module": True,
     "questions": [
        {"id": "wl_reservation_system", "label": "Reservation system", "type": "text"},
        {"id": "wl_waitlist_system", "label": "Waitlist system", "type": "choice",
         "options": ["Paper / memory", "Host app", "Texting waitlist", "None needed"]},
        {"id": "wl_quoted_wait", "label": "Average quoted wait on a busy night (min)", "type": "integer", "min": 0},
        {"id": "wl_actual_wait", "label": "Actual average wait (min)", "type": "integer", "min": 0},
        {"id": "wl_quote_accuracy", "label": "How accurate are quoted waits?", "type": "rating"},
        {"id": "wl_track_walkaways", "label": "Do you track guests who leave because of the wait?", "type": "yesno"},
        {"id": "wl_walkaways_week", "label": "Estimated walkaway parties per week", "type": "integer", "min": 0,
         "say": "On a busy week, how many parties do you think walk because the wait is too long?"},
        {"id": "wl_party_size", "label": "Average party size", "type": "decimal", "min": 1},
        {"id": "wl_abandoned", "label": "Abandoned waitlist entries per week", "type": "integer", "min": 0},
        {"id": "wl_peak_times", "label": "Peak wait times (days / hours)", "type": "text"},
        {"id": "wl_turn_time", "label": "Average table turn (min)", "type": "integer", "min": 0},
        {"id": "wl_know_turn", "label": "Do you know your average turn time?", "type": "yesno"},
        {"id": "wl_no_shows_week", "label": "No-shows per week", "type": "integer", "min": 0},
        {"id": "wl_host_staffing", "label": "Host staffing on peak nights", "type": "text"},
        {"id": "wl_texting", "label": "Waitlist texting in use?", "type": "yesno"},
        {"id": "wl_table_status", "label": "Table status visibility for hosts", "type": "rating"},
        {"id": "wl_bottlenecks", "label": "Can you identify bottlenecks by day and time?", "type": "yesno"},
        {"id": "wl_lost_revenue_guess", "label": "How much revenue do you think walkaways cost you?", "type": "textarea"},
        {"id": "wl_manual", "label": "How manually is the waitlist managed?", "type": "rating",
         "say": "1 is fully automated, 5 is a clipboard and a lot of shouting."},
        {"id": "wl_complaints", "label": "Guest complaints about waits", "type": "choice",
         "options": ["Rare", "Occasional", "Frequent"]},
     ]},

    {"key": "operations", "label": "General Operations", "short": "Operations",
     "intro": "Visibility. What they see, when they see it, and what they only find out later.",
     "questions": [
        {"id": "ops_morning_numbers", "label": "What numbers do you check every morning?", "type": "textarea"},
        {"id": "ops_daily_reports", "label": "Daily manager reports?", "type": "yesno"},
        {"id": "ops_weekly_reports", "label": "Weekly owner reports?", "type": "yesno"},
        {"id": "ops_reports_which", "label": "What reports do managers send you?", "type": "textarea"},
        {"id": "ops_forecasting", "label": "Sales forecasting in place?", "type": "yesno"},
        {"id": "ops_report_lag", "label": "How long until you see last week's numbers?", "type": "choice",
         "options": ["Same day", "Next day", "A few days", "A week or more", "Month-end"]},
        {"id": "ops_comps_month", "label": "Comps + voids + discounts per month ($)", "type": "currency"},
        {"id": "ops_comps_monitored", "label": "Are comps, voids and refunds monitored by person?", "type": "choice",
         "options": ["Daily", "Weekly", "Occasionally", "No"]},
        {"id": "ops_cash_controls", "label": "Cash controls", "type": "rating"},
        {"id": "ops_vendor_mgmt", "label": "Vendor management", "type": "rating"},
        {"id": "ops_training", "label": "Training consistency", "type": "rating"},
        {"id": "ops_maintenance", "label": "Maintenance tracked?", "type": "yesno"},
        {"id": "ops_accountability", "label": "Manager accountability to numbers", "type": "rating",
         "say": "1–5, how much do managers own their numbers?"},
        {"id": "ops_visibility", "label": "Your overall visibility into the operation", "type": "rating"},
        {"id": "ops_spreadsheets", "label": "How much runs on manual spreadsheets?", "type": "choice",
         "options": ["Nothing", "A little", "A lot", "Everything"]},
        {"id": "ops_hours_data", "label": "Hours per week you personally spend on data and reports", "type": "decimal", "min": 0},
        {"id": "ops_wish_sooner", "label": "What do you wish you knew sooner?", "type": "textarea"},
        {"id": "ops_surprises", "label": "What usually catches you by surprise?", "type": "textarea"},
        {"id": "ops_too_long", "label": "What takes too long to figure out?", "type": "textarea"},
        {"id": "ops_intuition", "label": "Which decisions still rely on intuition?", "type": "textarea"},
        {"id": "ops_leaking", "label": "Where do you think money is leaking?", "type": "textarea"},
        {"id": "ops_fix_tomorrow", "label": "What would you fix tomorrow if you could?", "type": "textarea"},
        {"id": "ops_least_visibility", "label": "Where do you have the least visibility?", "type": "textarea"},
     ]},

    {"key": "technology", "label": "Technology & Software", "short": "Technology",
     "intro": "What they pay for, what they use, and what overlaps.",
     "questions": [
        {"id": "tech_tools", "label": "Software stack", "type": "tools",
         "rows": ["POS", "Scheduling", "Payroll", "Accounting", "Inventory", "Reputation", "Marketing", "CRM",
                  "Loyalty", "Reservations", "Waitlist", "Analytics / reporting", "Other"]},
        {"id": "tech_use", "label": "Which tools do you actually use?", "type": "textarea"},
        {"id": "tech_ignored", "label": "Which ones do managers ignore?", "type": "textarea"},
        {"id": "tech_manual_export", "label": "Which tools require manual exporting?", "type": "textarea"},
        {"id": "tech_no_talk", "label": "Which systems don't talk to each other?", "type": "textarea"},
        {"id": "tech_overlap", "label": "Paying for overlapping functionality?", "type": "yesno"},
        {"id": "tech_dashboards", "label": "How many dashboards do you check?", "type": "integer", "min": 0},
     ]},

    {"key": "priorities", "label": "Owner Priorities", "short": "Priorities",
     "intro": "Let them talk. This is where the close comes from.",
     "questions": [
        {"id": "pri_top3", "label": "Top 3 concerns right now", "type": "textarea"},
        {"id": "pri_losing_money", "label": "Where do you feel you're losing money?", "type": "textarea"},
        {"id": "pri_improve_year", "label": "What are you trying to improve this year?", "type": "textarea"},
        {"id": "pri_metric", "label": "What metric do you watch most closely?", "type": "text"},
        {"id": "pri_surprises", "label": "What keeps surprising you?", "type": "textarea"},
        {"id": "pri_managers_struggle", "label": "What do managers struggle with?", "type": "textarea"},
        {"id": "pri_auto_info", "label": "What information do you wish you had automatically?", "type": "textarea"},
        {"id": "pri_least_efficient", "label": "Where is the operation least efficient?", "type": "textarea"},
        {"id": "pri_necessity", "label": "What would make Cavnar AI a necessity for you?", "type": "textarea"},
        {"id": "pri_three_things", "label": "If you could automatically know 3 things every morning, what would they be?", "type": "textarea"},
        {"id": "pri_notes", "label": "Freeform notes", "type": "textarea", "big": True},
     ]},
]

# Computed views rendered by the workspace after the question sections.
COMPUTED_SECTIONS = [
    {"key": "summary", "label": "Opportunity Summary", "short": "Summary"},
    {"key": "recommendation", "label": "Cavnar AI Recommendation", "short": "Recommendation"},
    {"key": "results", "label": "Final Audit Results", "short": "Results"},
]

# Internal-only sales observations. Never leave the admin side.
SALES_FIELDS = [
    {"id": "pain_level", "label": "Pain level", "type": "rating"},
    {"id": "decision_maker", "label": "Decision-maker", "type": "choice", "options": ["Yes, sole", "Yes, with partner", "Influencer only", "Unclear"]},
    {"id": "interest", "label": "Interest level", "type": "rating"},
    {"id": "budget_concern", "label": "Budget concern", "type": "choice", "options": ["None", "Some", "Major"]},
    {"id": "objections", "label": "Objections raised", "type": "textarea"},
    {"id": "timing", "label": "Timing", "type": "choice", "options": ["Now", "This quarter", "This year", "Unclear"]},
    {"id": "competitors", "label": "Competitors / tools in play", "type": "text"},
    {"id": "follow_up_needed", "label": "Follow-up needed", "type": "textarea"},
    {"id": "close_probability", "label": "Close probability (%)", "type": "percent"},
    {"id": "next_step", "label": "Next step", "type": "text"},
    {"id": "follow_up_date", "label": "Follow-up date", "type": "date"},
]

STATUSES = ["Draft", "In Progress", "Completed", "Follow-Up", "Closed / Converted", "Archived"]

# Answering these moves the completion % — long-form text isn't required
# for a complete audit, so it doesn't count against it.
SCORED_TYPES = {"currency", "percent", "integer", "decimal", "yesno", "choice", "multi", "rating", "date"}


def question_index():
    idx = {}
    for s in SECTIONS:
        for q in s["questions"]:
            idx[q["id"]] = dict(q, section=s["key"])
    return idx


QUESTIONS = question_index()


def is_visible(q, answers):
    """A follow-up hides until its trigger has the right answer. "*" means
    "anything non-empty"."""
    cond = q.get("show_if")
    if not cond:
        return True
    for qid, want in cond.items():
        val = answers.get(qid)
        if want == "*":
            if val in (None, "", [], {}):
                return False
        elif isinstance(want, list):
            if val not in want:
                return False
        elif val != want:
            return False
    return True


def completion(answers):
    """(answered, asked, per-section) over the questions currently visible
    and countable. 'Unknown' counts as answered — the owner was asked."""
    unknown = set((answers or {}).get("_unknown") or [])
    total = done = 0
    per = {}
    for s in SECTIONS:
        st = sd = 0
        for q in s["questions"]:
            if q["type"] not in SCORED_TYPES or not is_visible(q, answers):
                continue
            st += 1
            v = answers.get(q["id"])
            if q["id"] in unknown or v not in (None, "", [], {}):
                sd += 1
        per[s["key"]] = {"done": sd, "total": st, "pct": int(round(100.0 * sd / st)) if st else 0}
        total += st
        done += sd
    return done, total, per


def public_schema():
    return {"sections": SECTIONS, "computed": COMPUTED_SECTIONS, "sales": SALES_FIELDS, "statuses": STATUSES}
