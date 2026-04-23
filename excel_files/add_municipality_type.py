import pandas as pd

# ====================== OFFICIAL LISTS FROM YOUR PDF (kept for fallback) ======================
metropolitan_cities = {"kathmandu", "lalitpur", "bharatpur", "pokhara", "biratnagar", "birgunj"}
sub_metropolitan_cities = {"dharan", "itahari", "hetauda", "butwal", "siddharthanagar", "dhangadhi",
                           "tulsipur", "ghorahi", "janakpur", "kirtipur", "madhyapur thimi"}
municipalities = {  # normalised names from your PDF
    "inaruwa", "duhabi", "rajbiraj", "lahan", "siraha", "triyuga", "diktel rupakot majhuwagadhi",
    "phungling", "ilam", "birtamod", "damak", "mechinagar", "urlabari", "belbari", "rangeli",
    "sundar haraicha", "letang bhogateni", "budhabare", "kankai", "phidim", "taplejung", "khandbari",
    "chainpur", "bhojpur", "dhankuta", "pakhribas", "hile", "tehrathum", "myanglung", "solududhkunda",
    "salleri", "kalaiya", "gaur", "malangwa", "jaleshwar", "lalbandi", "bardibas", "mirchaiya",
    "hanumannagar kankalini", "golbazar", "kamala", "chandranigahapur", "garuda", "gadhimai",
    "simraungarh", "kolhabi", "ishworpur", "kariyamai", "pokhariya", "bindabasini", "dewahi gonahi",
    "baudihawa", "sursand", "pipra", "bidur", "trisuli", "belkotgadhi", "dupcheshwar", "suryagadhi",
    "kakani", "kageshwari manohara", "budhanilkantha", "gokarneshwor", "tokha", "tarakeshwor",
    "chandragiri", "dakshinkali", "konjyosom", "mahalaxmi", "godawari", "bagmati", "makwanpurgadhi",
    "thaha", "manahari", "raksirang", "kailash", "bhimphedi", "dhulikhel", "panauti", "panchkhal",
    "namobuddha", "charikot", "dolakha", "jiri", "gaurishankar", "ramechhap", "manthali", "doramba",
    "likhu tamakoshi", "sindhuli", "kamalamai", "sunkoshi", "tinpatan", "golanjor", "hariharpurgadhi",
    "dudhauli", "chautara sangachowk gadhi", "balephi", "helambu", "jugal", "bhotekoshi", "indrawati",
    "melamchi", "nuwakot", "tadi", "panchpokhari thangpaldhap", "waling", "putalibazar", "bhirkot",
    "arjunchaupari", "galyang", "harinas", "biruwa", "annapurna", "machhapuchchhre", "rupa", "madi",
    "beshishahar", "rainas", "sundarbazar", "dordi", "dudhpokhari", "marsyangdi", "gorkha", "palungtar",
    "arughat", "barpak sulikot", "tsum nubri", "siranchok", "ajirkot", "manang ngisyang", "mustang",
    "gharapjhong", "lomanthang", "thasang", "baglung", "dhorpatan", "bareng", "kanthekhola",
    "nisikhola", "jaimini", "burtibang", "myagde", "bandipur", "bhimad", "dulegaunda", "nawlpur",
    "aanbukhaireni", "rishing", "kushma", "phalewas", "modi", "painyu", "tansen", "rampur", "ribdikot",
    "nisdi", "tinau", "rambha", "mathagadhi", "devdaha", "saljhandi", "omsatiya", "sammarimai",
    "kanchan", "marchawari", "sunwal", "pratappur", "lamahi", "shantinagar", "rajpur", "babai",
    "rapti", "barbardiya", "bheriganga", "geruwa", "bansgadhi", "thakurbaba", "badhaiatal",
    "krishnanagar", "narainapur", "kohalpur", "raptisonari", "kapilvastu", "banganga", "maharajgunj",
    "shivaraj", "buddhabhumi", "yashodhara", "bijaynagar", "suddhodhan", "palhi nandan", "rohini",
    "pyuthan", "sarumarani", "mallarani", "naubahini", "mandavi", "jhimruk", "arghakhanchi",
    "sandhikharka", "panini", "bhumekasthan", "chhatradev", "jumla", "chandannath", "tatopani",
    "tila", "sinja", "kanakasundari", "hima", "dunai", "thuli bheri", "tripurasundari", "she phoksundo",
    "jagadulla", "mudkechula", "dolpo buddha", "simkot", "namkha", "soru", "chhayanath rara",
    "khatyad", "kalikot", "raskot", "tilagufa", "pachaljharana", "palata", "naraharinath",
    "sanni triveni", "dailekh", "dullu", "bhagawatimai", "dungeshwar", "chamunda bindrasaini",
    "gurans", "aathabis", "mahabu", "naumule", "bhairabi", "rukum east", "bhume", "sisne", "jajarkot",
    "barekot", "shivalaya", "nalgad", "chhedagad", "kushe", "birendranagar", "gurbhakot", "lekbeshi",
    "panchapuri", "bhimdatta", "shuklaphanta", "bedkot", "daijee", "punarbas", "mahakali",
    "lamkichuha", "ghodaghodi", "tikapur", "bhajani", "joshipur", "bardagoriya", "chure",
    "dipayal silgadhi", "bogtan fudsil", "purbichauki", "badikedar", "jorayal", "sayal", "shikhar",
    "mangalsen", "sanphebagar", "ramaroshan", "dhakari", "bannigadhi jayagadh", "mellekh",
    "chaurpati", "turmakhand", "jayaprithvi", "bungal", "talkot", "khaptad chhanna", "masta",
    "chhapiya", "durgathali", "kedarsyu", "saipal", "budhinanda", "tribeni", "himali", "badimalika",
    "budhiganga", "gaumul", "swamikartik khapar", "dasharathchand", "melauli", "patan (baitadi)",
    "purchaudi", "surnaya", "sigas", "dogadakedar", "dilasaini", "shailyashikhar", "naugad",
    "malikarjun", "apihimal", "duhun", "dunhu", "lekam", "marma"
}

def normalize(s):
    if pd.isna(s) or not isinstance(s, str):
        return ""
    s = s.strip().lower()
    s = s.replace("metropolitian", "metropolitan")
    s = s.replace(" municipality", "").replace(" rural municipality", "")
    s = s.replace(" metropolitan city", "").replace(" sub metropolitan", "")
    return ' '.join(s.split())

def classify_address(addr):
    if pd.isna(addr) or not isinstance(addr, str):
        return "RM"
    
    text = str(addr).lower()
    
    # Keyword priority (most reliable for your data)
    if "rural municipality" in text:
        return "RM"
    if "metropolitan" in text or "metropolitian city" in text:
        return "MP"
    if "sub-metropolitan" in text or "sub metropolitan" in text:
        return "Sub MP"
    if "municipality" in text:          # catches Shambhunath Municipality, Suryodaya, Rajpur, etc.
        return "MC"
    
    # Fallback to PDF list (only if no keyword found)
    norm = normalize(addr)
    if norm in metropolitan_cities:
        return "MP"
    if norm in sub_metropolitan_cities:
        return "Sub MP"
    if norm in municipalities:
        return "MC"
    
    return "RM"

# ====================== MAIN ======================
if __name__ == "__main__":
    input_file = "EXCEL TEST.xlsx"
    df = pd.read_excel(input_file, sheet_name="Sheet1")
    
    print(f"Rows loaded: {len(df):,}")
    
    df["Municipality_Type"] = ""
    
    for idx, row in df.iterrows():
        # Priority: Address3 first, then Address1
        typ = classify_address(row.get("Address3"))
        if typ == "RM":
            typ = classify_address(row.get("Address1"))
        df.at[idx, "Municipality_Type"] = typ
    
    output_file = "EXCEL TEST_with_municipality_FIXED.xlsx"
    df.to_excel(output_file, index=False)
    
    print(f"\n✅ DONE! Fixed file saved as:\n   {output_file}")
    print(df[["Address3", "Address1", "Municipality_Type"]].head(10).to_string())