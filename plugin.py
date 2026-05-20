from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
import os
from .dialog import EGMSDialog


class EGMS_TS_Concatenation:

    def __init__(self, iface):
        self.iface      = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action     = None

    def initGui(self):
        icon_path   = os.path.join(self.plugin_dir, "icon.png")
        self.action = QAction(
            QIcon(icon_path),
            "EGMS Time Series Concatenation",
            self.iface.mainWindow()
        )
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&EGMS Tools", self.action)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu("&EGMS Tools", self.action)

    def run(self):
        # Instantiate fresh each time so the layer combos are always up to date
        dlg = EGMSDialog(self.iface)
        dlg.exec()
