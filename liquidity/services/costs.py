"""Полная стоимость предложений и их сравнение для заёмщика."""


def offer_cost(offer, application):
    """Полная стоимость предложения.

    Проценты начисляются на сумму к выдаче за срок заявки, комиссия за организацию (кредитора + платформы)
    берётся один раз. «Полная стоимость, % годовых» = ставка + комиссия, разнесённая на срок:
    так предложения с разной ставкой и комиссией сравниваются одной цифрой.
    """
    amount = float(offer.funding_amount)
    rate = float(offer.annual_interest_rate)
    commission_pct = float(offer.total_commission)
    term = application.term_months
    commission = amount * commission_pct / 100
    interest = amount * rate / 100 * term / 12 if term else None
    return {
        "amount": amount,
        "coverage": round(100 * amount / float(application.funding_amount)) if application.funding_amount else 100,
        "interest": interest,
        "commission": commission,
        "overpayment": interest + commission if interest is not None else None,
        "effective_rate": rate + commission_pct * 12 / term if term else None,
    }


def compare_offers(application, offers):
    """Строки сравнения, отсортированные от самого дешёвого; лучшее предложение и экономия против следующего.

    Экономия считается на одну и ту же сумму (сумму заявки): у предложений бывает разная сумма к выдаче,
    и разница абсолютных переплат тогда вводила бы в заблуждение.
    """
    rows = [{"offer": o, "cost": offer_cost(o, application)} for o in offers]
    rows.sort(key=lambda r: (r["cost"]["effective_rate"] is None, r["cost"]["effective_rate"] or 0))
    open_rows = [r for r in rows if r["offer"].status == "shown" and r["cost"]["effective_rate"] is not None]
    best_id, savings = None, None
    if len(open_rows) > 1:
        best_id = open_rows[0]["offer"].id
        rate_gap = open_rows[1]["cost"]["effective_rate"] - open_rows[0]["cost"]["effective_rate"]
        savings = float(application.funding_amount) * rate_gap / 100 * application.term_months / 12
    return rows, best_id, savings


def payment_schedule(offer, application):
    """График платежей по той же модели, что и полная стоимость: комиссия при выдаче,
    проценты ежемесячно на всю сумму, основной долг одним платежом в последний месяц.
    Сумма всех процентов и комиссии равна переплате из offer_cost."""
    term = application.term_months
    if not term:
        return None
    amount = float(offer.funding_amount)
    monthly_interest = amount * float(offer.annual_interest_rate) / 100 / 12
    commission = amount * float(offer.total_commission) / 100
    rows = [{"month": 0, "label": "При выдаче", "interest": 0.0, "principal": 0.0, "commission": commission,
             "payment": commission, "balance": amount}]
    for month in range(1, term + 1):
        principal = amount if month == term else 0.0
        rows.append({"month": month, "label": f"{month} мес.", "interest": monthly_interest, "principal": principal,
                     "commission": 0.0, "payment": monthly_interest + principal,
                     "balance": amount - principal})
    totals = {"interest": monthly_interest * term, "principal": amount, "commission": commission,
              "payment": monthly_interest * term + amount + commission}
    return {"rows": rows, "totals": totals}
