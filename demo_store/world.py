"""
demo_store.world — what Halsted & Rye is: the store, its rooms and tables,
its menu (every dish's recipe and every drink's pour), the people who work
there, and the suppliers it buys from. Everything fictional except the
public products and distributors a Chicago bar buys from (Tito's, Breakthru
Beverage Illinois, Sysco): a real bar's invoices name real products.

Sizes are a 160-seat Lakeview sports bar & grill doing about $4.2M a year:
an average check near $31 a guest, food about 30% of food sales at plate
cost, pour cost about 20%, hourly labor near 25% of sales before salaries.
None of it is any client's figure.
"""
from functools import lru_cache

CG = 7310
STORE_MID = "7310000000000000001"
STORE_NAME = "Halsted & Rye"
TIMEZONE = "America/Chicago"
LAT, LNG = 41.9431, -87.6493          # Lakeview, three blocks from Wrigley Field
TAX_RATE = 0.1175                     # Chicago restaurant tax, all-in
OPENED = "2021-05-14"

# RPOWER record ids are 19-digit strings; each list gets its own block.
_BLOCK = {"store": 0, "salestype": 1, "dep": 2, "cat": 3, "item": 4, "job": 5, "emp": 6, "reason": 7,
          "meal": 8, "pcenter": 9, "room": 10, "table": 11, "paymeth": 12, "pocat": 13, "pricelvl": 14,
          "pyrlcat": 15, "price": 16, "empjob": 17}


def mid(kind, n) -> str:
    return "73%02d%015d" % (_BLOCK[kind], int(n))


# ── sales types (salestype/getbycg) ──────────────────────────────────────────
# name, flags. RPOWER's own vocabulary: a void is an "Error Correct" line, a
# comp its own type, a discount a negative line of a discount type, a $0
# modifier a non-sale.
SALES_TYPES = [
    ("Sale", {"type_sale": 1, "impacts_sales": 1, "impacts_costs": 1}),
    ("Comp", {"type_comp": 1, "impacts_sales": 1, "impacts_costs": 1}),
    ("Discount", {"type_discnt": 1, "impacts_sales": 1}),
    ("Error Correct", {"type_error": 1, "impacts_sales": 1}),
    ("Non Sale", {"type_nonsale": 1}),
    ("Refund", {"type_refund": 1, "impacts_sales": 1}),
    ("Gratuity", {"type_grat": 1}),
]
ST = {name: mid("salestype", i + 1) for i, (name, _f) in enumerate(SALES_TYPES)}

# ── departments, categories ─────────────────────────────────────────────────
DEPARTMENTS = ["Food", "Beer", "Liquor", "Wine", "Beverage", "Discounts", "Other"]
DEP = {d: mid("dep", i + 1) for i, d in enumerate(DEPARTMENTS)}

# category: (department, is_bar)
CATEGORIES = {
    "Starters": ("Food", 0), "Wings": ("Food", 0), "Burgers": ("Food", 0), "Sandwiches": ("Food", 0),
    "Tacos": ("Food", 0), "Salads": ("Food", 0), "Entrees": ("Food", 0), "Flatbreads": ("Food", 0),
    "Sides": ("Food", 0), "Desserts": ("Food", 0), "Brunch": ("Food", 0), "Kids": ("Food", 0),
    "Catering": ("Food", 0), "(Food Add-Ons)": ("Food", 0), "(Food Modifiers)": ("Food", 0),
    "Draft Beer": ("Beer", 1), "Bottles & Cans": ("Beer", 1), "Beer Towers": ("Beer", 1), "Buckets": ("Beer", 1),
    "Cocktails": ("Liquor", 1), "Vodka": ("Liquor", 1), "Whiskey": ("Liquor", 1), "Tequila": ("Liquor", 1),
    "Rum & Gin": ("Liquor", 1), "Shots": ("Liquor", 1), "(Liquor Add-Ons)": ("Liquor", 1),
    "Wine by the Glass": ("Wine", 1), "Wine Bottles": ("Wine", 1),
    "N/A Beverages": ("Beverage", 0), "N/A Beer & Mocktails": ("Beverage", 1),
    "Discounts": ("Discounts", 0), "Merchandise": ("Other", 0), "Gift Cards": ("Other", 0),
}
CAT = {c: mid("cat", i + 1) for i, c in enumerate(CATEGORIES)}

# ── kitchen inventory (Back Office items) ───────────────────────────────────
# key: (name, gl, vendor, sku, purchase unit, per pack, pack unit, price per purchase unit)
FOOD_ITEMS = {
    "beef":        ("Ground Beef 80/20 Chuck Brisket Blend", "FOOD - MEAT", "Sysco Chicago", "4218805", "Case", 10, "Pound", 63.90),
    "wings":       ("Chicken Wings Jumbo 1&2 Joint Fresh", "FOOD - POULTRY", "Sysco Chicago", "1734490", "Case", 40, "Pound", 141.60),
    "breast":      ("Chicken Breast Boneless Skinless 6oz", "FOOD - POULTRY", "Sysco Chicago", "1734524", "Case", 40, "Pound", 131.20),
    "tenders":     ("Chicken Tenderloin Raw Fresh", "FOOD - POULTRY", "Sysco Chicago", "1734607", "Case", 10, "Pound", 40.90),
    "bacon":       ("Bacon Applewood Smoked Sliced 14/18", "FOOD - MEAT", "Sysco Chicago", "5021934", "Case", 15, "Pound", 81.75),
    "brisket":     ("Beef Brisket Choice Packer", "FOOD - MEAT", "Sysco Chicago", "4220156", "Case", 60, "Pound", 351.00),
    "short_rib":   ("Beef Short Rib Boneless Choice", "FOOD - MEAT", "Sysco Chicago", "4220412", "Case", 20, "Pound", 223.00),
    "pork":        ("Pork Butt Bone-In", "FOOD - MEAT", "Sysco Chicago", "4301877", "Case", 40, "Pound", 108.40),
    "beef_it":     ("Italian Beef Roast Cooked Thin Sliced", "FOOD - MEAT", "Sysco Chicago", "4389213", "Case", 20, "Pound", 142.80),
    "sausage":     ("Italian Sausage Link Mild 4oz", "FOOD - MEAT", "Sysco Chicago", "4301933", "Case", 10, "Pound", 43.90),
    "strip":       ("Beef Strip Loin Choice Steak 12oz", "FOOD - MEAT", "Sysco Chicago", "4220587", "Case", 15, "Pound", 226.50),
    "hot_dog":     ("Vienna Beef Hot Dog 6/1 Natural Casing", "FOOD - MEAT", "Sysco Chicago", "4410025", "Case", 10, "Pound", 61.20),
    "cod":         ("Cod Fillet Atlantic Skinless 6-8oz", "FOOD - SEAFOOD", "Fortune Fish & Gourmet", "FF20418", "Case", 10, "Pound", 112.40),
    "salmon":      ("Salmon Fillet Atlantic Skin-On 6oz", "FOOD - SEAFOOD", "Fortune Fish & Gourmet", "FF21155", "Case", 10, "Pound", 126.90),
    "shrimp":      ("Shrimp 16/20 Raw Peeled Tail-On", "FOOD - SEAFOOD", "Fortune Fish & Gourmet", "FF30716", "Case", 10, "Pound", 98.50),
    "romaine":     ("Romaine Hearts", "FOOD - PRODUCE", "Testa Produce", "TP1104", "Case", 22, "Pound", 39.60),
    "iceberg":     ("Lettuce Iceberg Shredded", "FOOD - PRODUCE", "Testa Produce", "TP1112", "Case", 20, "Pound", 32.40),
    "greens":      ("Spring Mix", "FOOD - PRODUCE", "Testa Produce", "TP1150", "Case", 3, "Pound", 18.75),
    "tomato":      ("Tomato Roma", "FOOD - PRODUCE", "Testa Produce", "TP2201", "Case", 25, "Pound", 37.25),
    "red_onion":   ("Onion Red Jumbo", "FOOD - PRODUCE", "Testa Produce", "TP2310", "Case", 25, "Pound", 25.50),
    "onion":       ("Onion Yellow Jumbo", "FOOD - PRODUCE", "Testa Produce", "TP2302", "Case", 50, "Pound", 35.50),
    "jalapeno":    ("Peppers Jalapeno", "FOOD - PRODUCE", "Testa Produce", "TP2415", "Case", 10, "Pound", 17.40),
    "avocado":     ("Avocado Hass 48ct", "FOOD - PRODUCE", "Testa Produce", "TP3001", "Case", 48, "Each", 64.80),
    "lime":        ("Limes 175ct", "FOOD - PRODUCE", "Testa Produce", "TP3110", "Case", 175, "Each", 43.75),
    "lemon":       ("Lemons 165ct", "FOOD - PRODUCE", "Testa Produce", "TP3115", "Case", 165, "Each", 47.85),
    "cilantro":    ("Cilantro", "FOOD - PRODUCE", "Testa Produce", "TP3402", "Case", 30, "Each", 27.90),
    "potato":      ("Potato Russet 70ct", "FOOD - PRODUCE", "Testa Produce", "TP4001", "Case", 50, "Pound", 33.50),
    "cabbage":     ("Cabbage Green", "FOOD - PRODUCE", "Testa Produce", "TP4110", "Case", 50, "Pound", 29.50),
    "celery":      ("Celery Sticks", "FOOD - PRODUCE", "Testa Produce", "TP4205", "Case", 10, "Pound", 21.40),
    "carrot":      ("Carrot Sticks", "FOOD - PRODUCE", "Testa Produce", "TP4210", "Case", 10, "Pound", 19.90),
    "mushroom":    ("Mushrooms Button Sliced", "FOOD - PRODUCE", "Testa Produce", "TP4302", "Case", 10, "Pound", 28.60),
    "cheddar":     ("Cheese Cheddar Sharp Sliced", "FOOD - DAIRY", "Sysco Chicago", "6120044", "Case", 20, "Pound", 88.80),
    "american":    ("Cheese American Yellow Sliced 160ct", "FOOD - DAIRY", "Sysco Chicago", "6120101", "Case", 5, "Pound", 20.25),
    "pepperjack":  ("Cheese Pepper Jack Sliced", "FOOD - DAIRY", "Sysco Chicago", "6120129", "Case", 6, "Pound", 30.06),
    "mozz":        ("Cheese Mozzarella Shredded", "FOOD - DAIRY", "Sysco Chicago", "6120150", "Case", 20, "Pound", 73.20),
    "blue":        ("Cheese Blue Crumbled", "FOOD - DAIRY", "Sysco Chicago", "6120218", "Case", 5, "Pound", 37.25),
    "parm":        ("Cheese Parmesan Grated", "FOOD - DAIRY", "Sysco Chicago", "6120226", "Case", 5, "Pound", 41.90),
    "queso":       ("Cheese Sauce Queso Blanco", "FOOD - DAIRY", "Sysco Chicago", "6120374", "Case", 384, "Fluid Ounce", 55.20),
    "sour_cream":  ("Sour Cream", "FOOD - DAIRY", "Sysco Chicago", "6120440", "Case", 5, "Pound", 13.45),
    "butter":      ("Butter Solid Unsalted", "FOOD - DAIRY", "Sysco Chicago", "6120515", "Case", 36, "Pound", 131.40),
    "eggs":        ("Eggs Large Grade A", "FOOD - DAIRY", "Sysco Chicago", "6120606", "Case", 180, "Each", 42.30),
    "cream":       ("Cream Heavy Whipping 40%", "FOOD - DAIRY", "Sysco Chicago", "6120648", "Case", 384, "Fluid Ounce", 46.90),
    "brioche":     ("Bun Brioche 4.5in Sliced", "FOOD - BAKERY", "Turano Baking Co", "TB0412", "Case", 72, "Each", 33.84),
    "roll":        ("Roll French 8in Sandwich", "FOOD - BAKERY", "Turano Baking Co", "TB0508", "Case", 72, "Each", 36.72),
    "dog_bun":     ("Bun Hot Dog Poppy Seed", "FOOD - BAKERY", "Turano Baking Co", "TB0610", "Case", 96, "Each", 27.36),
    "texas":       ("Bread Texas Toast Sliced", "FOOD - BAKERY", "Turano Baking Co", "TB0702", "Case", 120, "Each", 21.60),
    "tortilla":    ("Tortilla Flour 6in", "FOOD - DRY GOODS", "Sysco Chicago", "7310402", "Case", 360, "Each", 33.12),
    "flatbread":   ("Flatbread Stone Fired 8x10", "FOOD - FROZEN", "Sysco Chicago", "7311019", "Case", 60, "Each", 48.60),
    "fries":       ("Fries 3/8 Skin-On Frozen", "FOOD - FROZEN", "Sysco Chicago", "7311530", "Case", 30, "Pound", 47.70),
    "tots":        ("Potato Tater Tots Frozen", "FOOD - FROZEN", "Sysco Chicago", "7311548", "Case", 30, "Pound", 45.60),
    "rings":       ("Onion Rings Beer Battered Frozen", "FOOD - FROZEN", "Sysco Chicago", "7311556", "Case", 20, "Pound", 49.80),
    "pretzel":     ("Pretzel Bavarian Soft 10oz", "FOOD - FROZEN", "Sysco Chicago", "7311613", "Case", 24, "Each", 40.32),
    "cake":        ("Cake Chocolate Fudge Layer 14-cut", "FOOD - FROZEN", "Sysco Chicago", "7311720", "Case", 28, "Each", 65.24),
    "cheesecake":  ("Cheesecake New York 16-cut", "FOOD - FROZEN", "Sysco Chicago", "7311738", "Case", 32, "Each", 59.20),
    "ice_cream":   ("Ice Cream Vanilla Bean", "FOOD - FROZEN", "Sysco Chicago", "7311746", "Case", 384, "Fluid Ounce", 27.90),
    "oil":         ("Oil Fryer Canola Clear", "FOOD - DRY GOODS", "Sysco Chicago", "7312009", "Case", 560, "Fluid Ounce", 47.25),
    "flour":       ("Flour All Purpose", "FOOD - DRY GOODS", "Sysco Chicago", "7312108", "Case", 50, "Pound", 23.90),
    "panko":       ("Breadcrumb Panko", "FOOD - DRY GOODS", "Sysco Chicago", "7312116", "Case", 25, "Pound", 41.75),
    "hot_sauce":   ("Sauce Hot Cayenne Pepper", "FOOD - DRY GOODS", "Sysco Chicago", "7312207", "Case", 512, "Fluid Ounce", 53.76),
    "bbq":         ("Sauce BBQ Original", "FOOD - DRY GOODS", "Sysco Chicago", "7312215", "Case", 512, "Fluid Ounce", 49.92),
    "ranch":       ("Dressing Ranch Buttermilk", "FOOD - DRY GOODS", "Sysco Chicago", "7312223", "Case", 512, "Fluid Ounce", 58.88),
    "mayo":        ("Mayonnaise Extra Heavy", "FOOD - DRY GOODS", "Sysco Chicago", "7312231", "Case", 512, "Fluid Ounce", 41.98),
    "chips":       ("Tortilla Chips Yellow Cut", "FOOD - DRY GOODS", "Sysco Chicago", "7312306", "Case", 30, "Pound", 39.30),
    "rice":        ("Rice Long Grain Parboiled", "FOOD - DRY GOODS", "Sysco Chicago", "7312314", "Case", 50, "Pound", 34.50),
    "black_beans": ("Beans Black Low Sodium #10", "FOOD - DRY GOODS", "Sysco Chicago", "7312322", "Case", 648, "Ounce", 28.20),
    "marinara":    ("Sauce Marinara Fancy #10", "FOOD - DRY GOODS", "Sysco Chicago", "7312330", "Case", 600, "Fluid Ounce", 36.60),
    "giard":       ("Giardiniera Hot Chicago Style", "FOOD - DRY GOODS", "Sysco Chicago", "7312348", "Case", 512, "Fluid Ounce", 39.04),
    "pickles":     ("Pickle Chips Dill", "FOOD - DRY GOODS", "Sysco Chicago", "7312355", "Case", 640, "Fluid Ounce", 34.56),
    "maple":       ("Syrup Maple Blend", "FOOD - DRY GOODS", "Sysco Chicago", "7312363", "Case", 256, "Fluid Ounce", 31.36),
    "seasoning":   ("Seasoning Cajun Blackening", "FOOD - DRY GOODS", "Sysco Chicago", "7312371", "Case", 96, "Ounce", 27.84),
}

# Prep recipes: name -> (group, yield (amount, unit), [(ingredient, qty, unit), ...]).
# An ingredient is a FOOD_ITEMS key, or "@Prep Name".
PREP = {
    "Buffalo Sauce": ("PREP RECIPES > Sauces", (64, "Fluid Ounce"), [("hot_sauce", 48, "Fluid Ounce"), ("butter", 1, "Pound")]),
    "Halsted Sauce": ("PREP RECIPES > Sauces", (64, "Fluid Ounce"), [("mayo", 40, "Fluid Ounce"), ("pickles", 12, "Fluid Ounce"), ("bbq", 12, "Fluid Ounce")]),
    "Pico de Gallo": ("PREP RECIPES > Salsas", (96, "Ounce"), [("tomato", 4, "Pound"), ("red_onion", 1, "Pound"), ("jalapeno", 0.5, "Pound"), ("cilantro", 2, "Each"), ("lime", 6, "Each")]),
    "Guacamole": ("PREP RECIPES > Salsas", (64, "Ounce"), [("avocado", 12, "Each"), ("lime", 4, "Each"), ("red_onion", 0.5, "Pound"), ("cilantro", 1, "Each")]),
    "Smoked Brisket": ("PREP RECIPES > Smoked", (33, "Pound"), [("brisket", 60, "Pound"), ("seasoning", 16, "Ounce")]),
    "Pulled Pork": ("PREP RECIPES > Smoked", (24, "Pound"), [("pork", 40, "Pound"), ("seasoning", 12, "Ounce"), ("bbq", 32, "Fluid Ounce")]),
    "Slaw": ("PREP RECIPES > Sides", (12, "Pound"), [("cabbage", 10, "Pound"), ("carrot", 1, "Pound"), ("mayo", 32, "Fluid Ounce")]),
    "Braised Short Rib": ("PREP RECIPES > Braises", (13, "Pound"), [("short_rib", 20, "Pound"), ("onion", 4, "Pound"), ("carrot", 2, "Pound")]),
}

# Food menu: category -> [(POS name, price, [(ingredient, qty, unit)])]
FOOD_MENU = {
    "Starters": [
        ("Loaded Tots", 13.0, [("tots", 10, "Ounce"), ("queso", 3, "Fluid Ounce"), ("bacon", 1.5, "Ounce"), ("sour_cream", 1, "Ounce"), ("jalapeno", 0.5, "Ounce")]),
        ("Chips & Guac", 12.0, [("chips", 5, "Ounce"), ("@Guacamole", 5, "Ounce"), ("@Pico de Gallo", 2, "Ounce")]),
        ("Queso & Chips", 11.0, [("chips", 5, "Ounce"), ("queso", 6, "Fluid Ounce"), ("@Pico de Gallo", 2, "Ounce")]),
        ("Bavarian Pretzel", 14.0, [("pretzel", 1, "Each"), ("queso", 4, "Fluid Ounce"), ("butter", 0.5, "Ounce")]),
        ("Fried Pickles", 11.0, [("pickles", 6, "Fluid Ounce"), ("flour", 2, "Ounce"), ("ranch", 2, "Fluid Ounce"), ("oil", 1.5, "Fluid Ounce")]),
        ("Brisket Nachos", 17.0, [("chips", 7, "Ounce"), ("@Smoked Brisket", 4, "Ounce"), ("queso", 4, "Fluid Ounce"), ("@Pico de Gallo", 2, "Ounce"), ("jalapeno", 1, "Ounce"), ("sour_cream", 1.5, "Ounce")]),
        ("Shrimp Cocktail", 16.0, [("shrimp", 6, "Ounce"), ("lemon", 0.5, "Each")]),
        ("Chicken Tenders", 15.0, [("tenders", 8, "Ounce"), ("flour", 1.5, "Ounce"), ("fries", 5, "Ounce"), ("ranch", 2, "Fluid Ounce"), ("oil", 1.5, "Fluid Ounce")]),
    ],
    "Wings": [
        ("Wings 10 Pc", 17.0, [("wings", 20, "Ounce"), ("@Buffalo Sauce", 2.5, "Fluid Ounce"), ("celery", 1.5, "Ounce"), ("carrot", 1.5, "Ounce"), ("ranch", 2, "Fluid Ounce"), ("oil", 2, "Fluid Ounce")]),
        ("Wings 20 Pc", 31.0, [("wings", 40, "Ounce"), ("@Buffalo Sauce", 5, "Fluid Ounce"), ("celery", 3, "Ounce"), ("carrot", 3, "Ounce"), ("ranch", 4, "Fluid Ounce"), ("oil", 3.5, "Fluid Ounce")]),
        ("Boneless Wings", 15.0, [("breast", 10, "Ounce"), ("flour", 1.5, "Ounce"), ("@Buffalo Sauce", 2.5, "Fluid Ounce"), ("ranch", 2, "Fluid Ounce"), ("oil", 2, "Fluid Ounce")]),
        ("Wing Platter 50 Pc", 72.0, [("wings", 100, "Ounce"), ("@Buffalo Sauce", 12, "Fluid Ounce"), ("celery", 8, "Ounce"), ("carrot", 8, "Ounce"), ("ranch", 10, "Fluid Ounce"), ("oil", 8, "Fluid Ounce")]),
    ],
    "Burgers": [
        ("Halsted Smash Burger", 17.0, [("beef", 8, "Ounce"), ("american", 1.5, "Ounce"), ("brioche", 1, "Each"), ("@Halsted Sauce", 1, "Fluid Ounce"), ("pickles", 1, "Fluid Ounce"), ("onion", 1, "Ounce"), ("fries", 6, "Ounce")]),
        ("Bacon Cheddar Burger", 18.0, [("beef", 8, "Ounce"), ("cheddar", 1.5, "Ounce"), ("bacon", 1.5, "Ounce"), ("brioche", 1, "Each"), ("romaine", 0.5, "Ounce"), ("tomato", 1, "Ounce"), ("fries", 6, "Ounce")]),
        ("Rye Whiskey BBQ Burger", 19.0, [("beef", 8, "Ounce"), ("cheddar", 1.5, "Ounce"), ("rings", 3, "Ounce"), ("bbq", 1.5, "Fluid Ounce"), ("brioche", 1, "Each"), ("fries", 6, "Ounce")]),
        ("Jalapeno Burger", 18.0, [("beef", 8, "Ounce"), ("pepperjack", 1.5, "Ounce"), ("jalapeno", 1, "Ounce"), ("@Guacamole", 2, "Ounce"), ("brioche", 1, "Each"), ("fries", 6, "Ounce")]),
        ("Mushroom Swiss Burger", 18.0, [("beef", 8, "Ounce"), ("cheddar", 1.5, "Ounce"), ("mushroom", 2, "Ounce"), ("onion", 1.5, "Ounce"), ("brioche", 1, "Each"), ("fries", 6, "Ounce")]),
    ],
    "Sandwiches": [
        ("Italian Beef", 16.0, [("beef_it", 7, "Ounce"), ("roll", 1, "Each"), ("giard", 2, "Fluid Ounce"), ("fries", 6, "Ounce")]),
        ("Combo Beef & Sausage", 18.0, [("beef_it", 5, "Ounce"), ("sausage", 4, "Ounce"), ("roll", 1, "Each"), ("giard", 2, "Fluid Ounce"), ("fries", 6, "Ounce")]),
        ("Brisket Sandwich", 18.0, [("@Smoked Brisket", 6, "Ounce"), ("brioche", 1, "Each"), ("bbq", 1.5, "Fluid Ounce"), ("@Slaw", 2, "Ounce"), ("fries", 6, "Ounce")]),
        ("Nashville Hot Chicken", 17.0, [("breast", 7, "Ounce"), ("flour", 1.5, "Ounce"), ("hot_sauce", 1, "Fluid Ounce"), ("@Slaw", 2, "Ounce"), ("brioche", 1, "Each"), ("pickles", 1, "Fluid Ounce"), ("fries", 6, "Ounce"), ("oil", 1.5, "Fluid Ounce")]),
        ("Chicago Dog", 9.0, [("hot_dog", 2.7, "Ounce"), ("dog_bun", 1, "Each"), ("tomato", 1, "Ounce"), ("onion", 0.5, "Ounce"), ("pickles", 1, "Fluid Ounce"), ("fries", 5, "Ounce")]),
        ("Pulled Pork Sandwich", 16.0, [("@Pulled Pork", 6, "Ounce"), ("brioche", 1, "Each"), ("@Slaw", 2, "Ounce"), ("fries", 6, "Ounce")]),
        ("Chicken Club Wrap", 15.0, [("breast", 5, "Ounce"), ("bacon", 1, "Ounce"), ("tortilla", 2, "Each"), ("romaine", 1.5, "Ounce"), ("tomato", 1, "Ounce"), ("ranch", 1, "Fluid Ounce"), ("fries", 6, "Ounce")]),
    ],
    "Tacos": [
        ("Brisket Tacos", 16.0, [("@Smoked Brisket", 5, "Ounce"), ("tortilla", 3, "Each"), ("@Pico de Gallo", 2, "Ounce"), ("cilantro", 0.2, "Each"), ("lime", 0.5, "Each")]),
        ("Baja Fish Tacos", 17.0, [("cod", 5, "Ounce"), ("flour", 1, "Ounce"), ("tortilla", 3, "Each"), ("@Slaw", 2, "Ounce"), ("@Pico de Gallo", 1.5, "Ounce"), ("lime", 0.5, "Each"), ("oil", 1, "Fluid Ounce")]),
        ("Chicken Tinga Tacos", 15.0, [("breast", 5, "Ounce"), ("tortilla", 3, "Each"), ("@Pico de Gallo", 2, "Ounce"), ("sour_cream", 1, "Ounce"), ("lime", 0.5, "Each")]),
    ],
    "Salads": [
        ("Chopped Cobb", 16.0, [("romaine", 5, "Ounce"), ("breast", 4, "Ounce"), ("bacon", 1, "Ounce"), ("eggs", 1, "Each"), ("avocado", 0.5, "Each"), ("blue", 1, "Ounce"), ("tomato", 2, "Ounce"), ("ranch", 2, "Fluid Ounce")]),
        ("Caesar Salad", 13.0, [("romaine", 6, "Ounce"), ("parm", 1, "Ounce"), ("texas", 1, "Each"), ("mayo", 1.5, "Fluid Ounce"), ("lemon", 0.25, "Each")]),
        ("Southwest Salmon Salad", 19.0, [("greens", 4, "Ounce"), ("salmon", 6, "Ounce"), ("black_beans", 2, "Ounce"), ("@Pico de Gallo", 2, "Ounce"), ("avocado", 0.5, "Each"), ("chips", 1, "Ounce")]),
    ],
    "Entrees": [
        ("Beer Battered Fish & Chips", 21.0, [("cod", 8, "Ounce"), ("flour", 2, "Ounce"), ("fries", 7, "Ounce"), ("@Slaw", 3, "Ounce"), ("lemon", 0.5, "Each"), ("oil", 2.5, "Fluid Ounce")]),
        ("Braised Short Rib", 29.0, [("@Braised Short Rib", 8, "Ounce"), ("potato", 8, "Ounce"), ("butter", 1, "Ounce"), ("cream", 1, "Fluid Ounce"), ("carrot", 2, "Ounce")]),
        ("Grilled Salmon", 26.0, [("salmon", 7, "Ounce"), ("rice", 4, "Ounce"), ("greens", 2, "Ounce"), ("lemon", 0.5, "Each"), ("butter", 0.5, "Ounce")]),
        ("NY Strip Frites", 34.0, [("strip", 12, "Ounce"), ("fries", 7, "Ounce"), ("butter", 1, "Ounce"), ("greens", 1, "Ounce")]),
        ("Half Rack Ribs", 24.0, [("@Pulled Pork", 14, "Ounce"), ("bbq", 2.5, "Fluid Ounce"), ("fries", 6, "Ounce"), ("@Slaw", 3, "Ounce")]),
        ("Mac & Cheese Skillet", 17.0, [("cream", 4, "Fluid Ounce"), ("cheddar", 3, "Ounce"), ("mozz", 2, "Ounce"), ("flour", 4, "Ounce"), ("bacon", 1.5, "Ounce"), ("panko", 0.5, "Ounce")]),
    ],
    "Flatbreads": [
        ("Margherita Flatbread", 14.0, [("flatbread", 1, "Each"), ("marinara", 3, "Fluid Ounce"), ("mozz", 4, "Ounce"), ("tomato", 1.5, "Ounce")]),
        ("BBQ Chicken Flatbread", 15.0, [("flatbread", 1, "Each"), ("bbq", 2, "Fluid Ounce"), ("breast", 3.5, "Ounce"), ("mozz", 3.5, "Ounce"), ("red_onion", 1, "Ounce"), ("cilantro", 0.2, "Each")]),
        ("Italian Beef Flatbread", 16.0, [("flatbread", 1, "Each"), ("beef_it", 4, "Ounce"), ("giard", 1.5, "Fluid Ounce"), ("mozz", 4, "Ounce")]),
    ],
    "Sides": [
        ("Side Fries", 6.0, [("fries", 8, "Ounce"), ("oil", 1, "Fluid Ounce")]),
        ("Side Tots", 6.0, [("tots", 8, "Ounce"), ("oil", 1, "Fluid Ounce")]),
        ("Onion Rings", 7.0, [("rings", 7, "Ounce"), ("oil", 1, "Fluid Ounce")]),
        ("Side Slaw", 4.0, [("@Slaw", 5, "Ounce")]),
        ("Side Salad", 6.0, [("greens", 3, "Ounce"), ("tomato", 1, "Ounce"), ("ranch", 1.5, "Fluid Ounce")]),
    ],
    "Desserts": [
        ("Chocolate Fudge Cake", 10.0, [("cake", 1, "Each"), ("ice_cream", 3, "Fluid Ounce")]),
        ("NY Cheesecake", 9.0, [("cheesecake", 1, "Each")]),
        ("Skillet Cookie", 10.0, [("flour", 3, "Ounce"), ("butter", 2, "Ounce"), ("eggs", 0.5, "Each"), ("ice_cream", 3, "Fluid Ounce")]),
    ],
    "Brunch": [
        ("Brisket Hash", 17.0, [("@Smoked Brisket", 5, "Ounce"), ("potato", 6, "Ounce"), ("eggs", 2, "Each"), ("onion", 1, "Ounce")]),
        ("Chicken & Waffles", 18.0, [("breast", 7, "Ounce"), ("flour", 4, "Ounce"), ("eggs", 1, "Each"), ("maple", 2, "Fluid Ounce"), ("butter", 1, "Ounce"), ("oil", 1.5, "Fluid Ounce")]),
        ("Breakfast Burrito", 15.0, [("eggs", 3, "Each"), ("tortilla", 1, "Each"), ("sausage", 3, "Ounce"), ("tots", 3, "Ounce"), ("cheddar", 1.5, "Ounce"), ("@Pico de Gallo", 2, "Ounce")]),
        ("Steak & Eggs", 24.0, [("strip", 8, "Ounce"), ("eggs", 2, "Each"), ("potato", 6, "Ounce"), ("texas", 1, "Each")]),
        ("French Toast", 14.0, [("texas", 3, "Each"), ("eggs", 2, "Each"), ("cream", 2, "Fluid Ounce"), ("maple", 2, "Fluid Ounce"), ("butter", 1, "Ounce")]),
    ],
    "Kids": [
        ("Kids Tenders", 9.0, [("tenders", 4, "Ounce"), ("flour", 1, "Ounce"), ("fries", 4, "Ounce"), ("oil", 1, "Fluid Ounce")]),
        ("Kids Cheeseburger", 9.0, [("beef", 4, "Ounce"), ("american", 0.75, "Ounce"), ("brioche", 1, "Each"), ("fries", 4, "Ounce")]),
        ("Kids Mac & Cheese", 8.0, [("cream", 2, "Fluid Ounce"), ("cheddar", 2, "Ounce"), ("flour", 3, "Ounce")]),
    ],
    "Catering": [
        ("Game Day Wing Party Pack", 145.0, [("wings", 200, "Ounce"), ("@Buffalo Sauce", 24, "Fluid Ounce"), ("celery", 16, "Ounce"), ("carrot", 16, "Ounce"), ("ranch", 20, "Fluid Ounce"), ("oil", 16, "Fluid Ounce")]),
        ("Taco Bar for 20", 260.0, [("@Smoked Brisket", 60, "Ounce"), ("breast", 60, "Ounce"), ("tortilla", 60, "Each"), ("@Pico de Gallo", 32, "Ounce"), ("@Guacamole", 32, "Ounce"), ("chips", 40, "Ounce"), ("queso", 48, "Fluid Ounce")]),
    ],
    "(Food Add-Ons)": [
        ("Add Bacon", 3.0, [("bacon", 1.5, "Ounce")]),
        ("Add Avocado", 3.0, [("avocado", 0.5, "Each")]),
        ("Add Chicken", 6.0, [("breast", 5, "Ounce")]),
        ("Add Shrimp", 8.0, [("shrimp", 4, "Ounce")]),
        ("Extra Patty", 6.0, [("beef", 4, "Ounce")]),
        ("Sub Tots", 1.5, [("tots", 6, "Ounce")]),
        ("Sub Onion Rings", 2.5, [("rings", 6, "Ounce")]),
        ("Extra Ranch", 0.75, [("ranch", 2, "Fluid Ounce")]),
    ],
}

# $0 kitchen modifiers, rung as non-sales.
FOOD_MODIFIERS = ["No Onion", "No Tomato", "Medium Rare", "Medium", "Medium Well", "Well Done", "Extra Crispy",
                  "Sauce on Side", "Hot", "BBQ", "Garlic Parm", "Dry Rub", "Gluten Free Bun", "Allergy - Nuts"]

# ── bar products (Fintech) and the drinks poured from them ───────────────────
# key: (distributor, item code, Fintech description, size (oz), uom, units per case, case price, category)
DISTRIBUTORS = {
    "BB": ("Breakthru Beverage Illinois, LLC", "BTB"),
    "SG": ("Southern Glazer's Wine & Spirits of Illinois", "SGWS"),
    "LB": ("Lakeshore Beverage", "LKSB"),
    "CB": ("Chicago Beverage Systems", "CBS"),
    "WC": ("Windy City Distribution", "WCD"),
}
OZ_PER_LITER = 33.814
BAR_PRODUCTS = {
    # beer: kegs (half barrel 1984 oz, sixth 661 oz) and cases
    "old_style_keg":  ("CB", "10514", "OLD STYLE 1/2 BBL KEG", 1984, "KEG", 1, 129.00, "beer"),
    "miller_keg":     ("CB", "10220", "MILLER LITE 1/2 BBL KEG", 1984, "KEG", 1, 149.00, "beer"),
    "coors_keg":      ("CB", "10301", "COORS LIGHT 1/2 BBL KEG", 1984, "KEG", 1, 149.00, "beer"),
    "modelo_keg":     ("LB", "22870", "MODELO ESPECIAL 1/2 BBL KEG", 1984, "KEG", 1, 199.00, "beer"),
    "312_keg":        ("LB", "23115", "GOOSE ISLAND 312 URBAN WHEAT 1/2 BBL", 1984, "KEG", 1, 189.00, "beer"),
    "stella_keg":     ("LB", "21660", "STELLA ARTOIS 1/2 BBL KEG", 1984, "KEG", 1, 209.00, "beer"),
    "guinness_keg":   ("BB", "41027", "GUINNESS DRAUGHT 1/2 BBL KEG", 1984, "KEG", 1, 229.00, "beer"),
    "antihero_keg":   ("WC", "6011", "REVOLUTION ANTI-HERO IPA 1/2 BBL", 1984, "KEG", 1, 225.00, "beer"),
    "daisy_keg":      ("WC", "6204", "HALF ACRE DAISY CUTTER PALE ALE 1/6 BBL", 661, "KEG", 1, 99.00, "beer"),
    "seasonal_keg":   ("WC", "6112", "REVOLUTION SEASONAL ROTATOR 1/6 BBL", 661, "KEG", 1, 104.00, "beer"),
    "bud_light":      ("LB", "20012", "BUD LIGHT 12OZ BTL 4/6", 12, "CS", 24, 30.95, "beer"),
    "budweiser":      ("LB", "20004", "BUDWEISER 12OZ BTL 4/6", 12, "CS", 24, 30.95, "beer"),
    "ultra":          ("LB", "20515", "MICHELOB ULTRA 12OZ BTL 4/6", 12, "CS", 24, 33.45, "beer"),
    "corona":         ("LB", "22501", "CORONA EXTRA 12OZ BTL 4/6", 12, "CS", 24, 36.95, "beer"),
    "high_noon":      ("SG", "880241", "HIGH NOON VODKA SELTZER VARIETY 12OZ CAN 3/8", 12, "CS", 24, 47.90, "beer"),
    "old_style_can":  ("CB", "10530", "OLD STYLE 16OZ CAN 4/6", 16, "CS", 24, 25.40, "beer"),
    "heineken_zero":  ("LB", "24410", "HEINEKEN 0.0 NON-ALC 12OZ BTL 4/6", 12, "CS", 24, 31.95, "na"),
    "athletic":       ("WC", "6501", "ATHLETIC RUN WILD IPA NA 12OZ CAN 4/6", 12, "CS", 24, 38.40, "na"),
    # spirits
    "svedka":         ("SG", "201457", "SVEDKA VODKA 1L", OZ_PER_LITER, "BTL", 12, 13.75 * 12, "liquor"),
    "titos":          ("SG", "203301", "TITOS HANDMADE VODKA 1L", OZ_PER_LITER, "BTL", 12, 27.25 * 12, "liquor"),
    "ketel":          ("BB", "310228", "KETEL ONE VODKA 1L", OZ_PER_LITER, "BTL", 12, 31.50 * 12, "liquor"),
    "grey_goose":     ("BB", "310402", "GREY GOOSE VODKA 1L", OZ_PER_LITER, "BTL", 12, 41.25 * 12, "liquor"),
    "evan":           ("SG", "214410", "EVAN WILLIAMS BLACK BOURBON 1L", OZ_PER_LITER, "BTL", 12, 15.95 * 12, "liquor"),
    "jameson":        ("SG", "216775", "JAMESON IRISH WHISKEY 1L", OZ_PER_LITER, "BTL", 12, 36.75 * 12, "liquor"),
    "makers":         ("BB", "312640", "MAKERS MARK BOURBON 1L", OZ_PER_LITER, "BTL", 12, 37.50 * 12, "liquor"),
    "buffalo":        ("SG", "214980", "BUFFALO TRACE BOURBON 1L", OZ_PER_LITER, "BTL", 12, 32.95 * 12, "liquor"),
    "crown":          ("BB", "312855", "CROWN ROYAL CANADIAN WHISKY 1L", OZ_PER_LITER, "BTL", 12, 37.95 * 12, "liquor"),
    "fireball":       ("SG", "215502", "FIREBALL CINNAMON WHISKY 1L", OZ_PER_LITER, "BTL", 12, 21.95 * 12, "liquor"),
    "koval":          ("BB", "318011", "KOVAL RYE WHISKEY 750ML", 25.36, "BTL", 6, 39.50 * 6, "liquor"),
    "templeton":      ("SG", "217340", "TEMPLETON RYE 4YR 750ML", 25.36, "BTL", 6, 29.95 * 6, "liquor"),
    "malort":         ("BB", "318305", "JEPPSONS MALORT 750ML", 25.36, "BTL", 12, 21.50 * 12, "liquor"),
    "lunazul":        ("SG", "220114", "LUNAZUL BLANCO TEQUILA 1L", OZ_PER_LITER, "BTL", 12, 19.25 * 12, "liquor"),
    "espolon":        ("SG", "220660", "ESPOLON BLANCO TEQUILA 1L", OZ_PER_LITER, "BTL", 12, 27.95 * 12, "liquor"),
    "casamigos":      ("BB", "320148", "CASAMIGOS BLANCO TEQUILA 750ML", 25.36, "BTL", 6, 44.95 * 6, "liquor"),
    "patron":         ("BB", "320301", "PATRON SILVER TEQUILA 750ML", 25.36, "BTL", 12, 40.95 * 12, "liquor"),
    "bacardi":        ("BB", "322005", "BACARDI SUPERIOR RUM 1L", OZ_PER_LITER, "BTL", 12, 17.95 * 12, "liquor"),
    "captain":        ("SG", "222418", "CAPTAIN MORGAN SPICED RUM 1L", OZ_PER_LITER, "BTL", 12, 21.95 * 12, "liquor"),
    "tanqueray":      ("SG", "224107", "TANQUERAY LONDON DRY GIN 1L", OZ_PER_LITER, "BTL", 12, 29.75 * 12, "liquor"),
    "hendricks":      ("BB", "324550", "HENDRICKS GIN 750ML", 25.36, "BTL", 6, 33.25 * 6, "liquor"),
    "triple_sec":     ("SG", "226003", "DEKUYPER TRIPLE SEC 1L", OZ_PER_LITER, "BTL", 12, 8.95 * 12, "liquor"),
    "aperol":         ("SG", "226514", "APEROL APERITIVO 1L", OZ_PER_LITER, "BTL", 6, 26.95 * 6, "liquor"),
    "kahlua":         ("BB", "326220", "KAHLUA COFFEE LIQUEUR 1L", OZ_PER_LITER, "BTL", 12, 25.95 * 12, "liquor"),
    "campari":        ("SG", "226530", "CAMPARI APERITIVO 1L", OZ_PER_LITER, "BTL", 6, 31.95 * 6, "liquor"),
    "vermouth":       ("BB", "326615", "DOLIN ROUGE SWEET VERMOUTH 750ML", 25.36, "BTL", 6, 13.95 * 6, "liquor"),
    "baileys":        ("SG", "226701", "BAILEYS IRISH CREAM 1L", OZ_PER_LITER, "BTL", 12, 29.95 * 12, "liquor"),
    # wine
    "cab":            ("SG", "501133", "JOSH CELLARS CABERNET SAUVIGNON 750ML", 25.36, "BTL", 12, 11.50 * 12, "wine"),
    "pinot_noir":     ("SG", "501890", "MEIOMI PINOT NOIR 750ML", 25.36, "BTL", 12, 15.75 * 12, "wine"),
    "chard":          ("BB", "540221", "KENDALL-JACKSON VINTNERS RESERVE CHARDONNAY 750ML", 25.36, "BTL", 12, 12.75 * 12, "wine"),
    "pinot_grigio":   ("SG", "502417", "ECCO DOMANI PINOT GRIGIO 750ML", 25.36, "BTL", 12, 9.25 * 12, "wine"),
    "sauv_blanc":     ("BB", "540640", "KIM CRAWFORD SAUVIGNON BLANC 750ML", 25.36, "BTL", 12, 12.50 * 12, "wine"),
    "rose":           ("BB", "541115", "WHISPERING ANGEL ROSE 750ML", 25.36, "BTL", 12, 19.25 * 12, "wine"),
    "prosecco":       ("SG", "503020", "LA MARCA PROSECCO 750ML", 25.36, "BTL", 12, 11.25 * 12, "wine"),
}

# Drinks: category -> [(POS name, price, happy-hour price or None, [(product, oz), ...] or None)]
# None: a drink with no tracked product (fountain soda, coffee).
DRINK_MENU = {
    "Draft Beer": [
        ("Old Style Draft", 5.0, 3.0, [("old_style_keg", 16)]),
        ("Miller Lite Draft", 7.0, 5.0, [("miller_keg", 16)]),
        ("Coors Light Draft", 7.0, 5.0, [("coors_keg", 16)]),
        ("Modelo Draft", 8.0, 6.0, [("modelo_keg", 16)]),
        ("312 Urban Wheat Draft", 8.0, 6.0, [("312_keg", 16)]),
        ("Stella Artois Draft", 8.5, 6.5, [("stella_keg", 16)]),
        ("Guinness Draft", 9.0, 7.0, [("guinness_keg", 20)]),
        ("Anti-Hero IPA Draft", 9.0, 7.0, [("antihero_keg", 16)]),
        ("Daisy Cutter Draft", 9.0, 7.0, [("daisy_keg", 16)]),
        ("Revolution Seasonal Draft", 9.0, 7.0, [("seasonal_keg", 16)]),
    ],
    "Beer Towers": [
        ("Old Style Tower", 26.0, None, [("old_style_keg", 100)]),
        ("Miller Lite Tower", 32.0, None, [("miller_keg", 100)]),
        ("Modelo Tower", 38.0, None, [("modelo_keg", 100)]),
    ],
    "Bottles & Cans": [
        ("Bud Light", 6.0, 4.0, [("bud_light", 12)]),
        ("Budweiser", 6.0, 4.0, [("budweiser", 12)]),
        ("Michelob Ultra", 6.5, 4.5, [("ultra", 12)]),
        ("Corona", 7.0, 5.0, [("corona", 12)]),
        ("High Noon", 8.0, 6.0, [("high_noon", 12)]),
        ("Old Style Tallboy", 5.0, 4.0, [("old_style_can", 16)]),
    ],
    "Buckets": [
        ("Bud Light Bucket", 28.0, None, [("bud_light", 60)]),
        ("Michelob Ultra Bucket", 30.0, None, [("ultra", 60)]),
        ("High Noon Bucket", 36.0, None, [("high_noon", 60)]),
    ],
    "Cocktails": [
        ("Halsted Old Fashioned", 15.0, None, [("koval", 2.0)]),
        ("Rye Mule", 13.0, None, [("templeton", 1.5)]),
        ("Spicy Margarita", 14.0, None, [("espolon", 1.5), ("triple_sec", 0.75)]),
        ("House Margarita", 12.0, 9.0, [("lunazul", 1.5), ("triple_sec", 0.75)]),
        ("Paloma", 13.0, None, [("espolon", 1.75)]),
        ("Espresso Martini", 15.0, None, [("ketel", 1.5), ("kahlua", 0.75)]),
        ("Aperol Spritz", 13.0, None, [("aperol", 2.0), ("prosecco", 3.0)]),
        ("Negroni", 14.0, None, [("tanqueray", 1.0), ("campari", 1.0), ("vermouth", 1.0)]),
        ("Wrigleyville Lemonade", 12.0, 9.0, [("svedka", 1.5)]),
        ("Long Island Iced Tea", 13.0, None, [("svedka", 0.5), ("tanqueray", 0.5), ("bacardi", 0.5), ("lunazul", 0.5), ("triple_sec", 0.5)]),
        ("Bloody Mary", 12.0, None, [("titos", 1.5)]),
    ],
    "Vodka": [
        ("Well Vodka", 8.0, 5.0, [("svedka", 1.5)]),
        ("Titos", 11.0, 8.0, [("titos", 1.5)]),
        ("Ketel One", 12.0, None, [("ketel", 1.5)]),
        ("Grey Goose", 13.0, None, [("grey_goose", 1.5)]),
    ],
    "Whiskey": [
        ("Well Whiskey", 8.0, 5.0, [("evan", 1.5)]),
        ("Jameson", 11.0, 8.0, [("jameson", 1.5)]),
        ("Makers Mark", 12.0, None, [("makers", 1.5)]),
        ("Buffalo Trace", 11.0, None, [("buffalo", 1.5)]),
        ("Crown Royal", 12.0, None, [("crown", 1.5)]),
        ("Koval Rye", 13.0, None, [("koval", 1.5)]),
    ],
    "Tequila": [
        ("Well Tequila", 8.0, 5.0, [("lunazul", 1.5)]),
        ("Espolon Blanco", 11.0, None, [("espolon", 1.5)]),
        ("Casamigos Blanco", 14.0, None, [("casamigos", 1.5)]),
        ("Patron Silver", 13.0, None, [("patron", 1.5)]),
    ],
    "Rum & Gin": [
        ("Bacardi", 9.0, 6.0, [("bacardi", 1.5)]),
        ("Captain Morgan", 9.0, None, [("captain", 1.5)]),
        ("Tanqueray", 11.0, None, [("tanqueray", 1.5)]),
        ("Hendricks", 13.0, None, [("hendricks", 1.5)]),
    ],
    "Shots": [
        ("Malort Shot", 6.0, None, [("malort", 1.5)]),
        ("Fireball Shot", 7.0, 5.0, [("fireball", 1.5)]),
        ("Jameson Shot", 9.0, None, [("jameson", 1.5)]),
        ("Green Tea Shot", 8.0, None, [("jameson", 0.75)]),
        ("Lemon Drop Shot", 8.0, None, [("svedka", 1.0), ("triple_sec", 0.5)]),
        ("Baileys", 9.0, None, [("baileys", 2.0)]),
    ],
    "(Liquor Add-Ons)": [
        ("Make It a Double", 5.0, None, None),
        ("Rocks", 0.0, None, None),
        ("Neat", 0.0, None, None),
        ("Soda", 0.0, None, None),
        ("Red Bull Mixer", 3.0, None, None),
    ],
    "Wine by the Glass": [
        ("Cabernet Glass", 12.0, 9.0, [("cab", 6)]),
        ("Pinot Noir Glass", 14.0, None, [("pinot_noir", 6)]),
        ("Chardonnay Glass", 12.0, 9.0, [("chard", 6)]),
        ("Pinot Grigio Glass", 11.0, 8.0, [("pinot_grigio", 6)]),
        ("Sauvignon Blanc Glass", 13.0, None, [("sauv_blanc", 6)]),
        ("Rose Glass", 14.0, None, [("rose", 6)]),
        ("Prosecco Glass", 11.0, 8.0, [("prosecco", 6)]),
        ("Mimosa", 9.0, None, [("prosecco", 4.0)]),
    ],
    "Wine Bottles": [
        ("Cabernet Bottle", 44.0, None, [("cab", 25.36)]),
        ("Pinot Noir Bottle", 52.0, None, [("pinot_noir", 25.36)]),
        ("Chardonnay Bottle", 44.0, None, [("chard", 25.36)]),
        ("Rose Bottle", 56.0, None, [("rose", 25.36)]),
        ("Prosecco Bottle", 40.0, None, [("prosecco", 25.36)]),
    ],
    "N/A Beverages": [
        ("Coke", 4.0, None, None), ("Diet Coke", 4.0, None, None), ("Sprite", 4.0, None, None),
        ("Iced Tea", 4.0, None, None), ("Lemonade", 4.5, None, None), ("Coffee", 3.5, None, None),
        ("Red Bull", 5.0, None, None), ("Kids Drink", 2.5, None, None),
    ],
    "N/A Beer & Mocktails": [
        ("Heineken 0.0", 6.0, None, [("heineken_zero", 12)]),
        ("Athletic Run Wild", 7.0, None, [("athletic", 12)]),
        ("Nojito", 9.0, None, None),
    ],
}

# Discounts (negative lines), by POS name: (rate, who it is for).
DISCOUNTS = [("50% Staff Meal", 0.50), ("Industry 20%", 0.20), ("First Responder 15%", 0.15),
             ("Manager 100% Comp", 1.0), ("Birthday Dessert", None), ("Gift Card Promo $10", None)]
OTHER_ITEMS = {"Merchandise": [("Halsted & Rye Tee", 25.0), ("Logo Pint Glass", 12.0)],
               "Gift Cards": [("Gift Card", 50.0)]}

# Void and comp reasons (voidreason/getbycg). made: the food was made.
REASONS = [("Rang In Wrong", "void", False), ("Guest Changed Mind", "void", False),
           ("Wrong Check", "void", False), ("Manager Test", "void", False),
           ("Kitchen Error", "comp", True), ("Guest Unhappy", "comp", True), ("Long Ticket Time", "comp", True),
           ("Manager Comp", "comp", False), ("Object in Food", "comp", True), ("Walk Out", "comp", False)]
REASON = {name: mid("reason", i + 2) for i, (name, _k, _m) in enumerate(REASONS)}   # "1" is "no reason"

MEALTIMES = [("Brunch", 0), ("Lunch", 1), ("Happy Hour", 2), ("Dinner", 3), ("Late Night", 4)]
MEAL = {n: mid("meal", i + 1) for i, (n, _s) in enumerate(MEALTIMES)}
PROFIT_CENTERS = ["Bar", "Dining", "Patio", "Takeout", "Catering"]
PCENTER = {n: mid("pcenter", i + 1) for i, n in enumerate(PROFIT_CENTERS)}

# rooms: name -> (is_bar, tables, profit center, seasonal)
ROOMS = {
    "Main Bar": (1, ["Bar %d" % i for i in range(1, 25)], "Bar", False),
    "Bar Rail": (1, ["Rail %d" % i for i in range(1, 9)], "Bar", False),
    "Dining Room": (0, ["%d" % i for i in range(10, 36)], "Dining", False),
    "Back Room": (0, ["%d" % i for i in range(40, 52)], "Dining", False),
    "Patio": (0, ["P%d" % i for i in range(1, 17)], "Patio", True),
    "Takeout": (0, ["To Go"], "Takeout", False),
}

PAYMENT_METHODS = [("Visa", 0, 1, "CRDT"), ("MasterCard", 0, 1, "CRDT"), ("American Express", 0, 1, "CRDT"),
                   ("Discover", 0, 1, "CRDT"), ("Cash", 1, 0, "CASH"), ("Gift Card", 0, 0, "GIFT"),
                   ("Apple Pay", 0, 1, "CRDT")]
PAYOUT_CATEGORIES = ["Cash Short/Over", "Paid Out - Supplies", "Paid Out - DJ", "Paid Out - Repairs"]
PRICE_LEVELS = ["Level 1", "Happy Hour"]

# ── people ───────────────────────────────────────────────────────────────────
# job: (ext_id, base rate, tipped, salaried, can_server)
JOBS = {
    "General Manager": ("GM01", 0.0, False, True, False),
    "Manager": ("MGR2", 26.0, False, False, False),
    "Kitchen Manager": ("KM03", 27.0, False, False, False),
    "Sous Chef": ("SC04", 23.0, False, False, False),
    "Line Cook": ("LC05", 19.5, False, False, False),
    "Prep Cook": ("PC06", 18.0, False, False, False),
    "Dishwasher": ("DW07", 17.0, False, False, False),
    "Server": ("SV08", 13.5, True, False, True),
    "Bartender": ("BT09", 13.5, True, False, True),
    "Barback": ("BB10", 15.0, True, False, False),
    "Host": ("HS11", 16.5, False, False, False),
    "Busser": ("BU12", 14.0, True, False, False),
    "Owner": ("OWN1", 0.0, False, True, False),
}
JOB = {name: mid("job", i + 1) for i, name in enumerate(JOBS)}

# (first, last, primary job, rate offset, also works, weekly target hours, start date)
STAFF = [
    ("Maya", "Castellanos", "Owner", 0, (), 30, "2021-05-14"),
    ("Daniel", "Whitfield", "Owner", 0, (), 0, "2021-05-14"),
    ("Priya", "Raman", "General Manager", 0, ("Manager",), 50, "2022-03-01"),
    ("Marcus", "Bell", "Manager", 0, (), 42, "2023-06-12"),
    ("Tessa", "Okonkwo", "Manager", 1.0, (), 40, "2022-09-19"),
    ("Grant", "Holloway", "Manager", -1.0, ("Bartender",), 34, "2025-02-03"),
    ("Luis", "Ortega", "Kitchen Manager", 0, (), 46, "2021-05-14"),
    ("Andre", "Kimura", "Sous Chef", 0, ("Line Cook",), 44, "2023-01-09"),
    ("Rafael", "Montoya", "Line Cook", 1.5, (), 43, "2021-08-02"),
    ("Keisha", "Dunmore", "Line Cook", 1.0, (), 38, "2022-11-14"),
    ("Tomas", "Villanueva", "Line Cook", 0.5, ("Prep Cook",), 40, "2023-04-24"),
    ("Brandon", "Szymanski", "Line Cook", 0, (), 36, "2024-02-19"),
    ("Hector", "Galindo", "Line Cook", 0.5, (), 39, "2023-08-07"),
    ("Jamal", "Pritchett", "Line Cook", -0.5, (), 32, "2025-05-27"),
    ("Ana", "Sepulveda", "Line Cook", 0, ("Prep Cook",), 34, "2024-09-03"),
    ("Devon", "Marsh", "Line Cook", -1.0, (), 28, "2026-01-12"),
    ("Rosa", "Arellano", "Prep Cook", 0.5, (), 36, "2022-06-06"),
    ("Moises", "Cardenas", "Prep Cook", 0, ("Dishwasher",), 34, "2024-04-15"),
    ("Lupe", "Navarrete", "Prep Cook", 0, (), 30, "2025-03-10"),
    ("Edgar", "Fuentes", "Dishwasher", 0.5, (), 38, "2023-02-13"),
    ("Kofi", "Asante", "Dishwasher", 0, (), 34, "2024-07-22"),
    ("Isaiah", "Brooks", "Dishwasher", 0, (), 30, "2025-08-18"),
    ("Mateo", "Quintero", "Dishwasher", 0, (), 24, "2026-04-06"),
    ("Jordan", "Reyes", "Server", 0, (), 34, "2023-03-20"),
    ("Kayla", "Brennan", "Server", 0, (), 32, "2022-05-09"),
    ("Sofia", "Lindqvist", "Server", 0, ("Host",), 28, "2024-06-03"),
    ("Tyler", "Novak", "Server", 0, (), 30, "2024-01-15"),
    ("Brianna", "Coleman", "Server", 0, (), 26, "2025-04-21"),
    ("Nate", "Ferraro", "Server", 0, (), 30, "2023-10-02"),
    ("Aisha", "Mahmoud", "Server", 0, (), 24, "2025-09-08"),
    ("Caleb", "Ostrowski", "Server", 0, ("Busser",), 28, "2024-11-11"),
    ("Mia", "Delgado", "Server", 0, (), 22, "2026-02-23"),
    ("Logan", "Pierce", "Server", 0, (), 26, "2025-06-16"),
    ("Paige", "Sorensen", "Server", 0, (), 26, "2026-05-18"),
    ("Danielle", "Wu", "Bartender", 0, (), 36, "2022-04-11"),
    ("Chris", "Kowalczyk", "Bartender", 0, (), 38, "2021-05-14"),
    ("Jasmine", "Thornton", "Bartender", 0, ("Server",), 32, "2023-07-17"),
    ("Ryan", "Mulcahy", "Bartender", 0, (), 34, "2022-10-24"),
    ("Elena", "Petrova", "Bartender", 0, (), 28, "2024-05-06"),
    ("Derek", "Lam", "Bartender", 0, (), 30, "2024-12-02"),
    ("Hannah", "Bergstrom", "Bartender", 0, ("Server",), 24, "2025-07-14"),
    ("Camille", "Dubois", "Bartender", 0, (), 30, "2025-04-07"),
    ("Victor", "Ruiz", "Bartender", 0, ("Barback",), 28, "2025-12-01"),
    ("Omar", "Haddad", "Barback", 0, ("Busser",), 30, "2024-08-19"),
    ("Tre", "Simmons", "Barback", 0, (), 26, "2025-10-06"),
    ("Lily", "Nakamura", "Host", 0, (), 24, "2025-03-31"),
    ("Grace", "Adebayo", "Host", 0, (), 22, "2024-10-14"),
    ("Ella", "Harrington", "Host", 0, (), 18, "2026-03-09"),
    ("Zoe", "Halvorsen", "Host", 0, (), 20, "2025-11-03"),
    ("Javier", "Solis", "Busser", 0, (), 30, "2024-03-25"),
    ("Malik", "Greene", "Busser", 0, (), 26, "2025-05-12"),
    ("Owen", "Dvorak", "Busser", 0, ("Barback",), 22, "2026-01-26"),
    ("Nia", "Robinson", "Busser", 0, (), 20, "2025-12-08"),
]
# Shared POS logins (RPOWER keeps them as employee records): never people.
STATIONS = [("Bar", "Station", "Bartender"), ("Online", "Pickup", "Server")]

# The server whose comps run well above the rest (the loss-prevention story).
COMP_HEAVY = "Jordan Reyes"


def full_name(s) -> str:
    return f"{s[0]} {s[1]}"


@lru_cache(maxsize=1)
def employees() -> list:
    """[{mid, first, last, name, job, rate, also, hours, start, payroll_id, station}] — staff then stations."""
    out = []
    for i, s in enumerate(STAFF):
        first, last, job, off, also, hours, start = s
        base = JOBS[job][1]
        out.append({"mid": mid("emp", i + 1), "first": first, "last": last, "name": f"{first} {last}",
                    "job": job, "rate": round(base + off, 2) if base else 0.0, "also": list(also),
                    "hours": hours, "start": start, "payroll_id": "HR%04d" % (1001 + i), "station": False})
    for j, (first, last, job) in enumerate(STATIONS):
        out.append({"mid": mid("emp", 900 + j), "first": first, "last": last, "name": f"{first} {last}",
                    "job": job, "rate": 0.0, "also": [], "hours": 0, "start": OPENED,
                    "payroll_id": "ST%02d" % (j + 1), "station": True})
    return out


@lru_cache(maxsize=1)
def by_job() -> dict:
    out = {}
    for e in employees():
        if e["station"]:
            continue
        out.setdefault(e["job"], []).append(e)
        for j in e["also"]:
            out.setdefault(j, []).append(dict(e, job=j, rate=JOBS[j][1] or e["rate"], secondary=True))
    return out


# ── the menu, flattened ─────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def menu() -> list:
    """[{mid, name, category, department, price, hh_price, kind, recipe | pours, is_mod}] — every POS button."""
    out, n = [], 0

    def add(**k):
        nonlocal n
        n += 1
        k.setdefault("hh_price", None)
        k.setdefault("is_mod", 0)
        k["mid"] = mid("item", n)
        k["department"] = CATEGORIES[k["category"]][0]
        out.append(k)

    for cat, rows in FOOD_MENU.items():
        for name, price, recipe in rows:
            add(name=name, category=cat, price=price, kind="food", recipe=recipe,
                is_mod=1 if cat.startswith("(") else 0)
    for name in FOOD_MODIFIERS:
        add(name=name, category="(Food Modifiers)", price=0.0, kind="food_mod", is_mod=1)
    for cat, rows in DRINK_MENU.items():
        for name, price, hh, pours in rows:
            add(name=name, category=cat, price=price, hh_price=hh, kind="drink", pours=pours,
                is_mod=1 if cat.startswith("(") else 0)
    for name, rate in DISCOUNTS:
        add(name=name, category="Discounts", price=0.0, kind="discount", rate=rate)
    for cat, rows in OTHER_ITEMS.items():
        for name, price in rows:
            add(name=name, category=cat, price=price, kind="other")
    return out


@lru_cache(maxsize=1)
def item_by_name() -> dict:
    return {m["name"]: m for m in menu()}


def product_cost_per_oz(key) -> float:
    p = BAR_PRODUCTS[key]
    return p[6] / (p[3] * p[5])
