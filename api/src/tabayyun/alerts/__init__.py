"""Alerts (spec 031): rules over a run's findings and scores, and telling about them.

- `rules`: what a rule may be, and storing it;
- `matching`: whether a finding or a score matches a rule's condition (pure);
- `evaluate`: after a run, turn its matches into notifications, throttled per rule;
- `messages`: what a notification says, as an email and as a webhook request (pure);
- `delivery`: sending a notification that is due and recording the outcome.
"""
