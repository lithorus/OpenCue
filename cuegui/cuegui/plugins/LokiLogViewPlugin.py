
from qtpy import QtWidgets
from qtpy import QtCore

import cuegui.AbstractDockWidget
import string

PLUGIN_NAME = 'LokiLogView'
PLUGIN_CATEGORY = 'Other'
PLUGIN_DESCRIPTION = 'Displays Frame Log'
PLUGIN_PROVIDES = 'LokiLogViewPlugin'
PRINTABLE = set(string.printable)

class LokiLogViewWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        print("Init plugin")
        self.app = cuegui.app()
        # print(self.app.display_log_file_content)
        # self.app.display_log_file_content.connect(self._set_log_files)
        self._setupUi(self)

    def __del__(self):
        print("Destruction")

#    def _set_log_files(self, log_files):
#        print(log_files)
#        pass

    def _setupUi(self, Form):
        """
        Export from pyuic5
        """
        self.verticalLayout = QtWidgets.QVBoxLayout(Form)
        self.verticalLayout.setObjectName("verticalLayout")
        self.horizontalLayout = QtWidgets.QHBoxLayout()
        self.horizontalLayout.setObjectName("horizontalLayout")
        self.frameLogCombo = QtWidgets.QComboBox(Form)
        self.frameLogCombo.setObjectName("frameLogCombo")
        self.horizontalLayout.addWidget(self.frameLogCombo)
        self.wordWrapCheck = QtWidgets.QCheckBox(Form)
        self.wordWrapCheck.setObjectName("wordWrapCheck")
        self.horizontalLayout.addWidget(self.wordWrapCheck)
        self.refreshButton = QtWidgets.QPushButton(Form)
        self.refreshButton.setObjectName("refreshButton")
        self.horizontalLayout.addWidget(self.refreshButton)
        self.horizontalLayout.setStretch(0, 1)
        self.verticalLayout.addLayout(self.horizontalLayout)
        self.textEdit = QtWidgets.QTextEdit(Form)
        self.textEdit.setObjectName("textEdit")
        self.verticalLayout.addWidget(self.textEdit)
        self.horizontalLayout_2 = QtWidgets.QHBoxLayout()
        self.horizontalLayout_2.setObjectName("horizontalLayout_2")
        self.caseCheck = QtWidgets.QCheckBox(Form)
        self.caseCheck.setObjectName("caseCheck")
        self.horizontalLayout_2.addWidget(self.caseCheck)
        self.searchLine = QtWidgets.QLineEdit(Form)
        self.searchLine.setObjectName("searchLine")
        self.horizontalLayout_2.addWidget(self.searchLine)
        self.findButton = QtWidgets.QPushButton(Form)
        self.findButton.setObjectName("findButton")
        self.horizontalLayout_2.addWidget(self.findButton)
        self.nextButton = QtWidgets.QPushButton(Form)
        self.nextButton.setObjectName("nextButton")
        self.horizontalLayout_2.addWidget(self.nextButton)
        self.prevButton = QtWidgets.QPushButton(Form)
        self.prevButton.setObjectName("prevButton")
        self.horizontalLayout_2.addWidget(self.prevButton)
        self.verticalLayout.addLayout(self.horizontalLayout_2)

        self._retranslateUi(Form)
        # QtCore.QMetaObject.connectSlotsByName(Form)

    def _retranslateUi(self, Form):
        _translate = QtCore.QCoreApplication.translate
        Form.setWindowTitle(_translate("Form", "Form"))
        self.wordWrapCheck.setText(_translate("Form", "Word Wrap"))
        self.refreshButton.setText(_translate("Form", "Refresh"))
        self.caseCheck.setText(_translate("Form", "Aa"))
        self.searchLine.setPlaceholderText(_translate("Form", "Search log.."))
        self.nextButton.setText(_translate("Form", "Next"))
        self.findButton.setText(_translate("Form", "Find"))
        self.prevButton.setText(_translate("Form", "Prev"))

class LokiLogViewPlugin(cuegui.AbstractDockWidget.AbstractDockWidget):
    """
    Plugin for displaying the log file content from loki for the selected frame with
    the ability to perform regex-based search.
    """

    def __init__(self, parent=None):
        """
        Create a LogViewPlugin instance

        @param parent: The parent widget
        @type parent: QtWidgets.QWidget or None
        """
        cuegui.AbstractDockWidget.AbstractDockWidget.__init__(
            self, parent, PLUGIN_NAME, QtCore.Qt.RightDockWidgetArea)
        self.logview_widget = LokiLogViewWidget(self)

        # self.app.display_log_file_content.connect(self._set_log_files)
        self.layout().addWidget(self.logview_widget)
