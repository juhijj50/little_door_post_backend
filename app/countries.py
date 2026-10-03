"""Where the post can go.

The form offers a list, but a list in a dropdown is a suggestion — anyone can
post whatever they like straight at the API. This module is the rule, and the
form reads its list from here so the two cannot drift apart.

Why Europe is missing
---------------------
Nowhere in Europe, and it is a deliberate line rather than an oversight.

Two things stack up there. Consumer law across the EU, the EEA and the UK gives
a distance buyer fourteen days to withdraw from a contract for no reason and be
refunded in full — including the envelope already posted and its postage — and
that right follows the buyer, so it binds a two-person workshop in India the
moment it sells to Dublin. On top of that the EU began charging duty on
low-value imports in July 2026, which puts a customs bill on an envelope that
used to arrive free.

Neither is something a club this size can run a second returns-and-tax regime
for, so the whole continent is off the list. Drawing the line at "Europe"
rather than at "the EU, the EEA and the UK" is a simplification on purpose:
the borders of those three are not obvious to a reader choosing from a
dropdown, and a rule people can predict is worth more than a few extra
countries.
"""

from __future__ import annotations

# Not posted to. Geographic Europe, which is a wider net than the EU/EEA/UK —
# see the note above for why the simpler line was chosen.
EXCLUDED = {
    "Albania", "Andorra", "Austria", "Belarus", "Belgium",
    "Bosnia and Herzegovina", "Bulgaria", "Croatia", "Cyprus", "Czechia",
    "Denmark", "Estonia", "Finland", "France", "Germany", "Greece",
    "Hungary", "Iceland", "Ireland", "Italy", "Kosovo", "Latvia",
    "Liechtenstein", "Lithuania", "Luxembourg", "Malta", "Moldova", "Monaco",
    "Montenegro", "Netherlands", "North Macedonia", "Norway", "Poland",
    "Portugal", "Romania", "Russia", "San Marino", "Serbia", "Slovakia",
    "Slovenia", "Spain", "Sweden", "Switzerland", "Ukraine",
    "United Kingdom", "Vatican City",
}

# Everywhere else. Sovereign states plus the handful of territories people
# actually write as their country. India is deliberately not in here: it is the
# other half of the choice on the form, and is priced in rupees.
INTERNATIONAL = [
    "Afghanistan", "Algeria", "Angola", "Antigua and Barbuda", "Argentina",
    "Armenia", "Australia", "Azerbaijan", "Bahamas", "Bahrain", "Bangladesh",
    "Barbados", "Belize", "Benin", "Bhutan", "Bolivia", "Botswana", "Brazil",
    "Brunei", "Burkina Faso", "Burundi", "Cambodia", "Cameroon", "Canada", "Cape Verde",
    "Central African Republic", "Chad", "Chile", "China", "Colombia", "Comoros",
    "Costa Rica", "Cuba", "Democratic Republic of the Congo", "Djibouti",
    "Dominica", "Dominican Republic", "Ecuador", "Egypt", "El Salvador", "Equatorial Guinea",
    "Eritrea", "Eswatini", "Ethiopia", "Fiji", "Gabon", "Gambia", "Georgia",
    "Ghana", "Grenada", "Guatemala", "Guinea", "Guinea-Bissau", "Guyana", "Haiti",
    "Honduras", "Hong Kong", "Indonesia", "Iran", "Iraq", "Israel", "Ivory Coast",
    "Jamaica", "Japan", "Jordan", "Kazakhstan", "Kenya", "Kiribati", "Kuwait",
    "Kyrgyzstan", "Laos", "Lebanon", "Lesotho", "Liberia", "Libya", "Macau",
    "Madagascar", "Malawi", "Malaysia", "Maldives", "Mali", "Marshall Islands",
    "Mauritania", "Mauritius", "Mexico", "Micronesia", "Mongolia", "Morocco",
    "Mozambique", "Myanmar", "Namibia", "Nauru", "Nepal", "New Zealand", "Nicaragua",
    "Niger", "Nigeria", "North Korea", "Oman", "Pakistan", "Palau", "Palestine",
    "Panama", "Papua New Guinea", "Paraguay", "Peru", "Philippines", "Puerto Rico",
    "Qatar", "Republic of the Congo", "Rwanda", "Saint Kitts and Nevis", "Saint Lucia",
    "Saint Vincent and the Grenadines", "Samoa", "Sao Tome and Principe",
    "Saudi Arabia", "Senegal", "Seychelles", "Sierra Leone", "Singapore", "Solomon Islands",
    "Somalia", "South Africa", "South Korea", "South Sudan", "Sri Lanka", "Sudan",
    "Suriname", "Syria", "Taiwan", "Tajikistan", "Tanzania", "Thailand", "Timor-Leste",
    "Togo", "Tonga", "Trinidad and Tobago", "Tunisia", "Türkiye", "Turkmenistan",
    "Tuvalu", "Uganda", "United Arab Emirates", "United States", "Uruguay",
    "Uzbekistan", "Vanuatu", "Venezuela", "Vietnam", "Yemen", "Zambia", "Zimbabwe",
]

# Spellings people actually type, mapped to the one this list uses. A reader
# who writes "USA" should not be told their country does not exist.
ALIASES = {
    "usa": "United States", "us": "United States", "america": "United States",
    "united states of america": "United States",
    "uae": "United Arab Emirates", "emirates": "United Arab Emirates",
    "dubai": "United Arab Emirates", "abu dhabi": "United Arab Emirates",
    "uk": "United Kingdom", "great britain": "United Kingdom",
    "britain": "United Kingdom", "england": "United Kingdom",
    "scotland": "United Kingdom", "wales": "United Kingdom",
    "northern ireland": "United Kingdom",
    "turkey": "Türkiye", "korea": "South Korea",
    "republic of korea": "South Korea", "south korea": "South Korea",
    "holland": "Netherlands", "czech republic": "Czechia",
    "nz": "New Zealand", "ksa": "Saudi Arabia",
    "cote d'ivoire": "Ivory Coast", "côte d'ivoire": "Ivory Coast",
    "drc": "Democratic Republic of the Congo",
    "swaziland": "Eswatini", "burma": "Myanmar",
    "east timor": "Timor-Leste", "cabo verde": "Cape Verde",
    "vatican": "Vatican City", "the vatican": "Vatican City",
    "bharat": "India", "hindustan": "India",
}

_LOOKUP = {name.casefold(): name for name in (*INTERNATIONAL, *EXCLUDED, "India")}


def canonical(name: str | None) -> str | None:
    """The spelling this module uses, or None if it is not a country we know.

    Aliases are resolved first so "USA", "uk" and "Dubai" all land somewhere —
    including on the excluded list, which matters: somebody typing "England"
    deserves to be told we do not post there, not that England is not a place.
    """
    if not name:
        return None
    key = " ".join(str(name).split()).strip(" ,.").casefold()

    # "U.S.A." survives the strip above as "u.s.a", and "US" is written half a
    # dozen ways. Try it as typed, then with the full stops taken out, so the
    # alias table does not have to carry every punctuation of every spelling.
    for candidate in (key, key.replace(".", "")):
        resolved = _LOOKUP.get(ALIASES.get(candidate, candidate).casefold())
        if resolved:
            return resolved
    return None


def is_served(name: str | None) -> bool:
    return canonical(name) in set(INTERNATIONAL) | {"India"}


def refusal(name: str | None) -> str:
    """Why we cannot post there, in words a reader can act on."""
    resolved = canonical(name)
    if resolved in EXCLUDED:
        return (
            f"We are sorry — we do not post to {resolved}, or anywhere else in "
            "Europe. The tax and consumer-protection rules there are more than "
            "a two-person workshop in India can take on. Everywhere else on the "
            "list is open."
        )
    return (
        "That is not a country we recognise. Choose one from the list — and if "
        "yours is genuinely missing, tell us and we will add it."
    )
