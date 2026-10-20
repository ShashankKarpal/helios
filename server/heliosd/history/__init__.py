"""Single-source Phase 2: the history import from the raw Apple Health exports.

export_xml reads export.xml, sidefiles reads the ECG and route files and the
export's checksum manifest, workouts builds the canonical sessions, and
importer writes the schema v5 tables and the reconciliation report. The
command line is server/tools/import_history_phase2.py."""
