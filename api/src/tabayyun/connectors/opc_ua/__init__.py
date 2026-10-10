"""The `opc_ua` connector (spec 023): historical data from an OPC UA server."""

from tabayyun.connectors.opc_ua.config import OpcUaConfig, OpcUaCredentials
from tabayyun.connectors.opc_ua.connector import OpcUaConnector

__all__ = ["OpcUaConfig", "OpcUaConnector", "OpcUaCredentials"]
