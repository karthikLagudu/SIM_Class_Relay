from dataclasses import dataclass

FULL_DEMO_PASSWORD = "ClassRelayDemo!42"
FULL_DEMO_SEED_KEY = "full-demo:dataset"
FULL_DEMO_SEED_OPERATION = "seed_full_demo"
QUICK_DEMO_ASSIGNMENT_KEY = "seed:assignment:1"


@dataclass(frozen=True)
class DemoAccount:
    name: str
    email: str
    password: str


QUICK_DEMO_ACCOUNTS = {
    "teacher": DemoAccount("Maya Teacher", "teacher@demo.local", "Teacher123!"),
    "student1": DemoAccount("Aarav Student", "student1@demo.local", "Student123!"),
    "student2": DemoAccount("Diya Student", "student2@demo.local", "Student123!"),
    "coordinator": DemoAccount("Ravi Coordinator", "coordinator@demo.local", "Coordinator123!"),
    "outsider": DemoAccount("Nora Teacher", "teacher@northstar.demo.local", "Teacher123!"),
    "lead": DemoAccount("Leena Teaching Coordinator", "lead@demo.local", "Coordinator123!"),
}


@dataclass(frozen=True)
class FullDemoAccount(DemoAccount):
    school_roles: tuple[tuple[str, str], ...]
    student_classes: tuple[str, ...] = ()
    linked: bool = True

    @property
    def handle(self) -> str:
        return self.email.partition("@")[0]


def full_demo_accounts() -> tuple[FullDemoAccount, ...]:
    accounts = [
        FullDemoAccount(
            "Mira Sen",
            "mira@willowbend.example",
            FULL_DEMO_PASSWORD,
            (
                ("willowbend", "coordinator"),
                ("willowbend", "teacher"),
                ("cedarharbor", "coordinator"),
            ),
        ),
        FullDemoAccount(
            "Nell Brooks",
            "nell@cedarharbor.example",
            FULL_DEMO_PASSWORD,
            (("cedarharbor", "teacher"), ("meridiangrove", "coordinator")),
        ),
    ]
    for name, handle, school in (
        ("Arun Vale", "arun", "willowbend"),
        ("Jonah Reed", "jonah", "cedarharbor"),
        ("Tessa Lin", "tessa", "willowbend"),
        ("Evan Sol", "evan", "willowbend"),
        ("Luca Kim", "luca", "cedarharbor"),
        ("Pia Chen", "pia", "cedarharbor"),
        ("Sam Finch", "sam", "meridiangrove"),
        ("Amina Cole", "amina", "meridiangrove"),
        ("Eli Vale", "elivale", "meridiangrove"),
        ("Rose Nash", "rose", "meridiangrove"),
    ):
        accounts.append(
            FullDemoAccount(
                name, f"{handle}@{school}.example", FULL_DEMO_PASSWORD, ((school, "teacher"),)
            )
        )
    for name, handle, enrolled, linked in (
        ("Isha Rao", "isha", ("8A", "8B"), True),
        ("Dev Malik", "dev", ("8A",), False),
        ("Leena Das", "leena", ("8A",), True),
        ("Rafi Khan", "rafi", ("8A",), True),
        ("Nila Roy", "nila", ("8A",), False),
        ("Kavi Bose", "kavi", ("8A",), True),
        ("Meera Shah", "meera", ("8A",), True),
        ("Sana Paul", "sana", ("8B",), False),
        ("Tara Iyer", "tara", ("8B",), True),
        ("Omar Dey", "omar", ("8B",), False),
        ("Rin Mathew", "rin", ("8B",), True),
        ("Kit Avery", "kit", ("9H",), True),
        ("Noa Quinn", "noa", ("9H",), False),
        ("Eli Finch", "eli", ("9H",), True),
    ):
        school = "cedarharbor" if "9H" in enrolled else "willowbend"
        accounts.append(
            FullDemoAccount(
                name,
                f"{handle}@{school}.example",
                FULL_DEMO_PASSWORD,
                ((school, "student"),),
                enrolled,
                linked,
            )
        )
    given_names = [
        "Adi",
        "Bela",
        "Cato",
        "Deni",
        "Esme",
        "Faye",
        "Gio",
        "Hana",
        "Ida",
        "Jules",
        "Kai",
        "Lina",
        "Milo",
        "Nori",
        "Oona",
        "Pavel",
        "Quin",
        "Rumi",
        "Suri",
        "Tess",
        "Uma",
        "Vera",
        "Wren",
        "Xavi",
        "Yara",
    ]
    for school, prefix, surname, groups in (
        ("willowbend", "willow", "Rowan", ("8A",) * 4 + ("8B",) * 4 + ("8C",) * 6),
        ("cedarharbor", "cedar", "Wren", ("9H",) * 6 + ("9M",) * 8 + ("9D",) * 8),
        ("meridiangrove", "grove", "Ash", ("7A",) * 9 + ("7B",) * 8 + ("7C",) * 8),
    ):
        for number, classroom in enumerate(groups, start=1):
            accounts.append(
                FullDemoAccount(
                    f"{given_names[number - 1]} {surname}",
                    f"{prefix}{number:02d}@{school}.example",
                    FULL_DEMO_PASSWORD,
                    ((school, "student"),),
                    (classroom,),
                    number % 3 == 1,
                )
            )
    return tuple(accounts)


FULL_DEMO_ACCOUNTS = full_demo_accounts()
