"""
ServiceNow tool family for neuro-san.

Deployment-agnostic by construction: no base URL, endpoint path, table name or
credential appears anywhere in this package. Every such value is supplied at
runtime through the deployment profile (see profile.py).
"""
