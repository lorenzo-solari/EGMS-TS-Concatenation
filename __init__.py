def classFactory(iface):
    from .plugin import EGMS_TS_Concatenation
    return EGMS_TS_Concatenation(iface)