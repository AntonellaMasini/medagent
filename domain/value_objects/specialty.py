"""Medical specialty value object with Spanish/English normalization."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Specialty(str, Enum):
    """Specialties as represented in the Cigna cuadro médico autocomplete."""

    PSICOLOGIA = "PSICOLOGIA"
    PSIQUIATRIA = "PSIQUIATRIA"
    DERMATOLOGIA = "DERMATOLOGIA"
    GINECOLOGIA = "GINECOLOGIA"
    CARDIOLOGIA = "CARDIOLOGIA"
    OFTALMOLOGIA = "OFTALMOLOGIA"
    TRAUMATOLOGIA = "TRAUMATOLOGIA"
    ENDOCRINOLOGIA = "ENDOCRINOLOGIA"
    UROLOGIA = "UROLOGIA"
    OTORRINOLARINGOLOGIA = "OTORRINOLARINGOLOGIA"
    MEDICINA_GENERAL = "MEDICINA GENERAL"
    PEDIATRIA = "PEDIATRIA"
    NEUROLOGIA = "NEUROLOGIA"
    REUMATOLOGIA = "REUMATOLOGIA"
    DIGESTIVO = "DIGESTIVO"
    ALERGOLOGIA = "ALERGOLOGIA"
    ODONTOLOGIA = "ODONTOLOGIA"
    NUTRICION = "NUTRICION"
    FISIOTERAPIA = "FISIOTERAPIA"


# Free-text → canonical Specialty. Lowercase, accent-stripped lookup keys.
_ALIASES: dict[str, Specialty] = {
    # Psicología
    "psicologo": Specialty.PSICOLOGIA,
    "psicologa": Specialty.PSICOLOGIA,
    "psicologia": Specialty.PSICOLOGIA,
    "psychologist": Specialty.PSICOLOGIA,
    "psychology": Specialty.PSICOLOGIA,
    "therapist": Specialty.PSICOLOGIA,
    # Psiquiatría
    "psiquiatra": Specialty.PSIQUIATRIA,
    "psiquiatria": Specialty.PSIQUIATRIA,
    "psychiatrist": Specialty.PSIQUIATRIA,
    # Dermatología
    "dermatologo": Specialty.DERMATOLOGIA,
    "dermatologa": Specialty.DERMATOLOGIA,
    "dermatologia": Specialty.DERMATOLOGIA,
    "dermatologist": Specialty.DERMATOLOGIA,
    "skin doctor": Specialty.DERMATOLOGIA,
    # Ginecología
    "ginecologo": Specialty.GINECOLOGIA,
    "ginecologa": Specialty.GINECOLOGIA,
    "ginecologia": Specialty.GINECOLOGIA,
    "gynecologist": Specialty.GINECOLOGIA,
    "gyno": Specialty.GINECOLOGIA,
    # Cardiología
    "cardiologo": Specialty.CARDIOLOGIA,
    "cardiologa": Specialty.CARDIOLOGIA,
    "cardiologia": Specialty.CARDIOLOGIA,
    "cardiologist": Specialty.CARDIOLOGIA,
    # Oftalmología
    "oftalmologo": Specialty.OFTALMOLOGIA,
    "oftalmologia": Specialty.OFTALMOLOGIA,
    "ophthalmologist": Specialty.OFTALMOLOGIA,
    "eye doctor": Specialty.OFTALMOLOGIA,
    # Traumatología
    "traumatologo": Specialty.TRAUMATOLOGIA,
    "traumatologia": Specialty.TRAUMATOLOGIA,
    "traumatologist": Specialty.TRAUMATOLOGIA,
    "orthopedist": Specialty.TRAUMATOLOGIA,
    # Endocrinología
    "endocrino": Specialty.ENDOCRINOLOGIA,
    "endocrinologo": Specialty.ENDOCRINOLOGIA,
    "endocrinologia": Specialty.ENDOCRINOLOGIA,
    "endocrinologist": Specialty.ENDOCRINOLOGIA,
    # Urología
    "urologo": Specialty.UROLOGIA,
    "urologia": Specialty.UROLOGIA,
    "urologist": Specialty.UROLOGIA,
    # ORL
    "otorrino": Specialty.OTORRINOLARINGOLOGIA,
    "otorrinolaringologo": Specialty.OTORRINOLARINGOLOGIA,
    "otorrinolaringologia": Specialty.OTORRINOLARINGOLOGIA,
    "ent": Specialty.OTORRINOLARINGOLOGIA,
    # Medicina general
    "medico general": Specialty.MEDICINA_GENERAL,
    "medicina general": Specialty.MEDICINA_GENERAL,
    "general practitioner": Specialty.MEDICINA_GENERAL,
    "gp": Specialty.MEDICINA_GENERAL,
    "family doctor": Specialty.MEDICINA_GENERAL,
    # Pediatría
    "pediatra": Specialty.PEDIATRIA,
    "pediatria": Specialty.PEDIATRIA,
    "pediatrician": Specialty.PEDIATRIA,
    # Neurología
    "neurologo": Specialty.NEUROLOGIA,
    "neurologia": Specialty.NEUROLOGIA,
    "neurologist": Specialty.NEUROLOGIA,
    # Reumatología
    "reumatologo": Specialty.REUMATOLOGIA,
    "reumatologia": Specialty.REUMATOLOGIA,
    "rheumatologist": Specialty.REUMATOLOGIA,
    # Digestivo
    "digestivo": Specialty.DIGESTIVO,
    "gastroenterologo": Specialty.DIGESTIVO,
    "gastroenterologist": Specialty.DIGESTIVO,
    # Alergología
    "alergologo": Specialty.ALERGOLOGIA,
    "alergologia": Specialty.ALERGOLOGIA,
    "allergist": Specialty.ALERGOLOGIA,
    # Odontología
    "dentista": Specialty.ODONTOLOGIA,
    "odontologo": Specialty.ODONTOLOGIA,
    "odontologia": Specialty.ODONTOLOGIA,
    "dentist": Specialty.ODONTOLOGIA,
    # Nutrición
    "nutricionista": Specialty.NUTRICION,
    "nutricion": Specialty.NUTRICION,
    "nutritionist": Specialty.NUTRICION,
    "dietitian": Specialty.NUTRICION,
    # Fisioterapia
    "fisioterapeuta": Specialty.FISIOTERAPIA,
    "fisioterapia": Specialty.FISIOTERAPIA,
    "physiotherapist": Specialty.FISIOTERAPIA,
    "physical therapist": Specialty.FISIOTERAPIA,
}


_ACCENT_MAP = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")


@dataclass(frozen=True)
class SpecialtyMatch:
    """Result of normalizing a free-text specialty query."""

    specialty: Specialty
    raw: str


def normalize_specialty(query: str) -> SpecialtyMatch | None:
    """Map free-text user input to a canonical Specialty.

    Accent-insensitive, case-insensitive. Returns None if no match.
    """
    if not query:
        return None
    key = query.strip().lower().translate(_ACCENT_MAP)
    match = _ALIASES.get(key)
    if match is None:
        # Try direct enum value match (accent-stripped)
        for sp in Specialty:
            if sp.value.lower().translate(_ACCENT_MAP) == key:
                match = sp
                break
    if match is None:
        return None
    return SpecialtyMatch(specialty=match, raw=query)
