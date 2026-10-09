"""
Bulk-insert the complete NATO DIANA 2026 cohort (150 companies) into raw_leads.
Duplicates are silently skipped via the UNIQUE(company_name, source) constraint.

Source URL: https://www.diana.nato.int/about-diana/2026-cohort-of-companies.html

Run:
    python -m src.collectors.seed_diana_2026
"""

from collections import defaultdict

try:
    from db.migrate import migrate as migrate_database
except ImportError:  # supports `python -m src.collectors.seed_diana_2026`
    from src.db.migrate import migrate as migrate_database

from .base import DB_PATH
from .base import store_lead

SOURCE = "NATO DIANA 2026"
SOURCE_URL = "https://www.diana.nato.int/about-diana/2026-cohort-of-companies.html"

# (company_name, country_or_None)
# Country inferred from legal suffix only:
#   GmbH=Germany, AS=Norway, Oy=Finland, OÜ=Estonia, srl=Italy/Romania→null,
#   doo=Slovenia/Croatia→null, SL=Spain, SA=Greece, BV=Netherlands, AB=Sweden,
#   Ltd/Limited=UK, "Canada" in name=Canada. All others null.
COHORT: dict[str, list[tuple[str, str | None]]] = {
    "Advanced Communications": [
        ("Arke Telekom", None),
        ("Atlas Innovative Technologies", None),
        ("Beechat Network Systems", None),
        ("Belfort", None),
        ("Celare Quantum Communications", None),
        ("DeployX", None),
        ("Enclaive GmbH", "Germany"),
        ("JET Connectivity", None),
        ("NanTenna", None),
        ("Neuron Innovations", None),
        ("Pan Galactic Corporation", None),
        ("Qoherent", "Canada"),
        ("Tightbeam Photonics", "Canada"),
        ("Vidoc Security Lab", None),
        ("VISS", None),
    ],
    "Autonomy and Unmanned Systems": [
        ("AegisX", None),
        ("Aerix Systems", None),
        ("ALIAS ROBOTICS SL", "Spain"),
        ("Alpha Autonomy", None),
        ("Arcani", None),
        ("Delian Alliance Industries", None),
        ("DroneTector Limited", "United Kingdom"),
        ("Hydrogen in Motion", "Canada"),
        ("Mara", None),
        ("NeuralAgent", None),
        ("Picogrid", None),
        ("Pliant Energy Systems Inc", None),
        ("Robotto", None),
        ("Vizgard", None),
        ("Wave Sciences", None),
    ],
    "Contested Electromagnetic Spectrum": [
        ("AdamantQ", None),
        ("AmorphiQ", None),
        ("CX2", None),
        ("Evolunar", None),
        ("FASMETRICS SA", "Greece"),
        ("FOSSA Systems", None),
        ("LSMedical", None),
        ("Odysseus Space", None),
        ("OLEDCOMM", "France"),
        ("Perf Drone Systems", None),
        ("Rotonium", None),
        ("SDQ Solutions Canada", "Canada"),
        ("Slipstream Engineering Design Limited", "United Kingdom"),
        ("TERN", None),
        ("Testnor", "Norway"),
    ],
    "Critical Infrastructure and Logistics": [
        ("Connect Robotics", None),
        ("Copsys Technologies Inc", "Canada"),
        ("Canadian Strategic Missions Corporation", "Canada"),
        ("e-peas", None),
        ("Ethicronics", None),
        ("Fieldmade", None),
        ("Hydro Road Limited", "United Kingdom"),
        ("INCAS", None),
        ("Incendia Canada Inc", "Canada"),
        ("Knitronix", None),
        ("Niricson", "Canada"),
        ("PEK AUTOMOTIVE DOO", None),
        ("ROBOTINA doo", None),
        ("Safety Bolt AB", "Sweden"),
        ("Simularge", None),
    ],
    "Data and Decision Making": [
        ("Aereus", None),
        ("Aktiver", None),
        ("Blue Team Intelligence BV", "Netherlands"),
        ("C2GRID", None),
        ("CulturePulse", None),
        ("DATAMBIT Limited", "United Kingdom"),
        ("Datifex", "Canada"),
        ("Exentech Savunma", None),
        ("GlobVision Inc", "Canada"),
        ("HIGHTEK", None),
        ("InovecTech", None),
        ("NorthStar Earth & Space", "Canada"),
        ("SkyFi", None),
        ("VIG SEC DRONE SL", "Spain"),
        ("XRF", None),
    ],
    "Energy and Power": [
        ("Airloom Energy", None),
        ("ATOM H2", None),
        ("Avju Solutions AS", "Norway"),
        ("Boson Energy", None),
        ("CALYOS", None),
        ("Exeger Operations AB", "Sweden"),
        ("Exonetik", "Canada"),
        ("Flatlight", None),
        ("Grengine Inc", "Canada"),
        ("Helicoid Industries Inc", None),
        ("LUX", None),
        ("Novac", None),
        ("SolarinBlue", None),
        ("SOLARSTEAM INC", "Canada"),
        ("TAURiON Batteries GmbH", "Germany"),
    ],
    "Human Resilience and Biotechnology": [
        ("Aboa Space Research Oy", "Finland"),
        ("AUXSYS", None),
        ("Avivo Biomedical Inc", "Canada"),
        ("Beyond Blood Diagnostics", None),
        ("Biocellis", None),
        ("Cohesys", "Canada"),
        ("Deep Breathe Inc", "Canada"),
        ("Elitac Wearables", None),
        ("GutSee Health", None),
        ("Herges Detection GmbH", "Germany"),
        ("Hourglass Medical", None),
        ("LightOx", None),
        ("Lysando Innovations Lab GmbH", "Germany"),
        ("New Platelet Company NPC", None),
        ("ReBlood Rx", None),
    ],
    "Maritime Operations": [
        ("AMPHITRITE", None),
        ("ClearDrop", None),
        ("Composite Energy Technologies LLC", None),
        ("Eye2Drive", None),
        ("Hefring Marine", None),
        ("HYPERKELP", None),
        ("Lux Bio", "Canada"),
        ("MAPS Messaging BV", "Netherlands"),
        ("Oceano Robotics", None),
        ("Orpheus", None),
        ("Quantum Quest", None),
        ("StrateSea Technology Inc", None),
        ("Unplugged", None),
        ("Vatn Systems", None),
        ("VICTUS Technologies Inc", None),
    ],
    "Extreme Environments": [
        ("Alchemy", "Canada"),
        ("Aviant AS", "Norway"),
        ("Chimera Energy", None),
        ("Drone City", None),
        ("FireSwarm Solutions", "Canada"),
        ("GBatteries Energy Canada Inc", "Canada"),
        ("Hellstern medical GmbH", "Germany"),
        ("InfraHex sro", None),
        ("Marble Aerospace Limited", "United Kingdom"),
        ("Mesodyne", None),
        ("Microbium", None),
        ("SEATOM Technologies", None),
        ("SINTEG Systems", None),
        ("Spacedrip OÜ", "Estonia"),
        ("Volta Space Technologies", "Canada"),
    ],
    "Resilient Space Operations": [
        ("Adaptronics", None),
        ("Alba Orbital", None),
        ("Applied Atomics", None),
        ("BruhnBruhn Innovation", None),
        ("Capsule Corporation srl", None),
        ("Deep Space Energy", None),
        ("Ecosmic", None),
        ("Kreios Space", None),
        ("Mithril Technologies Inc", None),
        ("Neuraspace", None),
        ("Space Power Ltd", "United Kingdom"),
        ("Space Solar", None),
        ("Spaceflux Ltd", "United Kingdom"),
        ("Spectra Defence Limited", "United Kingdom"),
        ("WarpWare ATTX Inc", None),
    ],
}


def seed() -> None:
    migrate_database(DB_PATH)

    total = sum(len(v) for v in COHORT.values())
    print(f"Seeding {total} NATO DIANA 2026 companies into raw_leads...")

    area_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    total_new = total_dup = 0

    for area, companies in COHORT.items():
        for name, country in companies:
            inserted = store_lead(
                company_name=name,
                source=SOURCE,
                source_url=SOURCE_URL,
                initial_description=None,
                category_hint=area,
                country=country,
            )
            if inserted:
                area_counts[area][0] += 1
                total_new += 1
            else:
                area_counts[area][1] += 1
                total_dup += 1

    print(f"\n{'='*60}")
    print(f"Total in batch : {total}")
    print(f"New inserted   : {total_new}")
    print(f"Duplicates skip: {total_dup}")
    print(f"\nBreakdown by challenge area:")
    for area in COHORT:
        new, dup = area_counts[area]
        print(f"  {area:<45}  {new:3d} new  {dup:3d} dup")
    print("=" * 60)


if __name__ == "__main__":
    seed()
