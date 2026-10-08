"""Disposition table: every OTD K-1 semantic id -> what the bridge does with it.

Kinds
  direct    numeric value copied to an OpenTax k1_partnership field
  attested  copied only when the preparer facts file supplies the evidence the
            engine requires (otherwise the run is blocked)
  review    no faithful engine input exists; a named reviewer must record a
            decision in the facts file (otherwise the run is blocked)
  info      no Form 1040 effect within this bridge's scope; recorded only

An OTD id that is not in this table blocks the run (fail closed), so a new
taxonomy item can never be dropped silently.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rule:
    kind: str
    target: str | None = None  # OpenTax k1_partnership field
    box: str = ""
    sign: int = 1  # -1 where OTD carries deductions as negatives but OpenTax wants >= 0
    note: str = ""
    attest_key: str | None = None  # facts.attestations key for 'attested'
    sums_into: tuple[str, ...] = field(default_factory=tuple)


RULES: dict[str, Rule] = {
    # ---- Part III income ------------------------------------------------
    "ordinary_business_income": Rule("direct", "box1_ordinary_business", "1"),
    "net_rental_real_estate_income": Rule("direct", "box2_rental_re", "2"),
    "other_net_rental_income": Rule("direct", "box3_other_rental", "3"),
    "guaranteed_payments_services": Rule("direct", "box4a_guaranteed_services", "4a"),
    "guaranteed_payments_capital": Rule("direct", "box4b_guaranteed_capital", "4b"),
    "guaranteed_payments_total": Rule("info", box="4c", note="derived total of 4a+4b; engine computes it"),
    "interest_income": Rule("direct", "box5_interest", "5"),
    "ordinary_dividends": Rule("direct", "box6a_ordinary_dividends", "6a"),
    "qualified_dividends": Rule("direct", "box6b_qualified_dividends", "6b"),
    "dividend_equivalents": Rule("direct", "box6c_dividend_equivalents", "6c"),
    "royalties": Rule("attested", "box7_royalties", "7", attest_key="box7_royalty_reporting",
                      note="engine requires TSJ, property description and 1099 answers"),
    "net_short_term_capital_gain": Rule("direct", "box8_net_st_cap_gain", "8"),
    "net_long_term_capital_gain": Rule("direct", "box9a_net_lt_cap_gain", "9a"),
    "collectibles_gain": Rule("direct", "box9b_collectibles_gain", "9b"),
    "unrecaptured_section_1250_gain": Rule("direct", "box9c_unrecaptured_1250", "9c"),
    "net_section_1231_gain": Rule("direct", "box10_net_1231", "10"),
    "other_income.portfolio": Rule("review", box="11A", note="no coded 11A input in engine"),
    "other_income.section_1256_contracts": Rule("review", box="11C", note="needs Form 6781; not in engine K-1 node"),
    "other_income.non_portfolio_capital": Rule(
        "attested", None, "11S", attest_key="box11_code_s_nonportfolio_capital",
        note="engine needs the short/long-term split and character workpaper"),
    "section_179_deduction": Rule("review", box="12",
                                  note="engine's Form 4562 cannot use an aggregate-only 179 amount (needs asset rows)"),
    # ---- Box 13 deductions ----------------------------------------------
    "other_deductions.cash_contributions_60": Rule("review", box="13A", note="charitable items need categorized Schedule A source"),
    "other_deductions.cash_contributions_30": Rule("review", box="13B", note="charitable items need categorized Schedule A source"),
    "other_deductions.portfolio_deductions": Rule("review", box="13AE", note="IRC 67(g) suspends these for individuals through 2025"),
    "other_deductions.investment_interest": Rule("review", box="13H",
                                                 note="engine's Form 4952 needs explicit AMT refigure facts"),
    "other_deductions.pensions_iras": Rule("review", box="13R", note="partner-level retirement deduction needs plan detail"),
    # ---- 14-21 ----------------------------------------------------------
    "self_employment.net_earnings": Rule("direct", "box14a_se_earnings", "14A"),
    "credits.zero_emission_nuclear": Rule("review", box="15", note="credit code not supported by engine K-1 node"),
    "amt.depreciation_adjustment": Rule("review", box="17A",
                                        note="engine's Form 6251 needs line-specific AMT modeling, not a K-1 total"),
    "amt.adjusted_gain_loss": Rule("review", box="17B",
                                   note="engine's Form 6251 needs line-specific AMT modeling, not a K-1 total"),
    "tax_exempt.interest": Rule("direct", "box18_tax_exempt_income", "18A"),
    "nondeductible.expenses": Rule("review", box="18C",
                                   note="engine's single box18 field would add, not subtract, for basis"),
    "distributions.cash_marketable_securities": Rule("direct", "box19_distributions", "19A"),
    "other_information.investment_income": Rule("review", box="20A", note="Form 4952 input; not taken from K-1 node"),
    "other_information.investment_expenses": Rule("review", box="20B", note="engine's 20B object needs issuer crosswalk"),
    "other_information.gross_receipts_448c": Rule("info", box="20AG", note="Form 8990 small-business test only"),
    "other_information.section_199a": Rule("review", box="20Z", note="QBI needs issuer statement detail"),
    "foreign_taxes_paid_accrued": Rule("attested", "box16_foreign_tax", "21", attest_key="foreign_tax",
                                       note="engine needs category, kind, method, country and date"),
    "at_risk_activities": Rule("info", box="22"),
    "passive_activities": Rule("info", box="23"),
}

# Descriptive / identity items in Parts I and II. Recorded, never sent to the engine.
IDENTITY_PREFIXES = ("partnership.", "partner.")

# Lines the bridge can reconcile when the return holds only this K-1.
# Each expected value is computed from the values actually sent to the engine.
RECONCILE = {
    "line2b_taxable_interest": ("box5_interest",),
    "line3b_ordinary_dividends": ("box6a_ordinary_dividends",),
    "line3a_qualified_dividends": ("box6b_qualified_dividends",),
    "line7_capital_gain": ("box8_net_st_cap_gain", "box9a_net_lt_cap_gain", "box10_net_1231"),
    "line8_additional_income": ("box1_ordinary_business", "box2_rental_re", "box3_other_rental",
                                "box4a_guaranteed_services", "box4b_guaranteed_capital",
                                "box7_royalties"),
}

# Engine node -> K-1 boxes that feed it, so a node failure is reported in K-1 terms.
NODE_SOURCES = {
    "k1_partnership": "the whole K-1",
    "schedule_b": "boxes 5 / 6a (interest, dividends)",
    "form4952": "box 13H / 20A-B (investment interest)",
    "form4562": "box 12 (section 179)",
    "form6251": "box 17 (AMT items)",
    "form_1116": "box 21 (foreign taxes)",
    "schedule_d": "boxes 8 / 9a / 10 / 11S (capital gains)",
    "schedule_e": "boxes 1-4 / 7 (Schedule E income)",
}
