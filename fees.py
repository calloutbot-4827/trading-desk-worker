"""FOMO Solana fee model (per side). Gas 0."""

def side_fee(ticket_usd, discount=0.10):
    if ticket_usd <= 0:
        return 0.0
    if ticket_usd <= 47.50:   # spec band $5-$47.50; below $5 also 2% (unspecified)
        base = ticket_usd * 0.02
    elif ticket_usd <= 190:
        base = 0.95
    else:
        base = ticket_usd * 0.005
    return round(base * (1 - discount), 6)


def round_trip_fees(entry_usd, exit_usd=None, discount=0.10):
    exit_usd = entry_usd if exit_usd is None else exit_usd
    return round(side_fee(entry_usd, discount) + side_fee(exit_usd, discount), 6)
