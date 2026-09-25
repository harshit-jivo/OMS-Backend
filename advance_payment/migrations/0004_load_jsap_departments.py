"""Copy JSAP's departments and sub-departments into OMS, with JSAP's ids.

Taken from JSAP `GET /api/Hierarchy/GetDepartments` on 2026-09-24, because
JSAP is being shut down: after this, OMS's tables are the master list. Names
are copied exactly as JSAP had them (typos included), so this is a faithful
copy; rename them in OMS afterwards if wanted.

The ids are kept, so workflow queries (`department_id = 35` is Finance) and
the department ids in the employee export mean the same thing as in JSAP.
Afterwards the id sequences are moved past the highest copied id, so a
department or sub-department added in OMS gets the next free number.

41 departments, 97 sub-departments. Reversing deletes exactly these rows.
"""
from django.core.management.color import no_style
from django.db import migrations

#: (department id, name, [(sub-department id, name), ...])
DEPARTMENTS = [
    (1, 'Accounts & Finance', [
        (1, 'A/P'), (2, 'A/R'), (3, 'Accounts (MART)'), (4, 'All Payments'),
        (63, 'AP-Payment ARY'), (62, 'AP-Payment Ilahi'), (5, 'Compliance'),
        (60, 'Import/Export'), (6, 'MIS'), (7, 'Payments Process'), (61, 'Reconcilation'),
        (8, 'SAP')]),
    (25, 'Admin', [
        (84, 'Admin'), (66, 'D-38'), (85, 'Gardner'), (86, 'Housekeeping'), (67, 'It Hardware'),
        (65, 'Maintainance & Security'), (83, 'Pantry J-3'), (64, 'Pantry Mayapuri'),
        (68, 'Pantry Mayapuri Basement')]),
    (2, 'Admin (Maintainance and HouseKeeping)', [
        (48, 'Admin'), (9, 'IT Hardware'), (10, 'Maintainance & Security'), (11, 'Pantry Basement'),
        (12, 'Pantry J3'), (13, 'Pantry Mayapuri'), (47, 'sweeper')]),
    (3, 'Audit', [
        (14, 'A/P Audit'), (15, 'A/R Audit'), (72, 'Audit Dispatch'), (78, 'Central Audit-Invoice'),
        (79, 'Payment Audit- Wellnss/ARY/Ilahi')]),
    (4, 'Backend Sales', [
        (16, 'CSD'), (69, 'EA'), (17, 'GT Oil Sales'), (71, 'GT Sales'), (94, 'Instant Sales'),
        (49, 'International Business'), (18, 'MIS'), (19, 'Sales'), (20, 'Water/ Beverages')]),
    (38, 'Civil Enenerging', [(93, 'Maintenance & Structural Engg')]),
    (40, 'Cyber Security', []),
    (5, 'Design & Media', [(21, 'Media'), (22, 'Web Developement')]),
    (6, "Director's Desk", [(76, 'EA'), (23, 'PA')]),
    (28, 'Dispatch', [(82, 'GRPO')]),
    (29, 'EA', []),
    (7, 'ECom', [
        (24, 'Call Center'), (59, 'Mayapuri Warehouse'), (58, 'Media'), (25, 'Sales'),
        (26, 'Supply Chain')]),
    (32, 'Engineering &  Maintenance', []),
    (35, 'Finance', [
        (87, 'Accounts & Finance Management'), (92, 'AP'), (88, 'AR'), (91, 'Compliance'),
        (90, 'Payment Process'), (89, 'Reconcilation')]),
    (24, 'HR', [(56, 'HR Payroll'), (57, 'HR Recruitment')]),
    (22, 'HR & Payroll', [(50, 'Salary Payments')]),
    (9, 'HR & Recruitment', [(28, 'HR-Operations'), (55, 'Recruitment'), (29, 'Salary Payments')]),
    (23, 'International Business', []),
    (10, 'IT', [
        (51, 'IT hardware'), (53, 'SAP'), (30, 'Software'), (70, 'Software- Deveopment It'),
        (31, 'Web developer')]),
    (41, 'J-3', [(96, 'Sales- Store')]),
    (11, 'Labels & Print Media', [(32, 'Design & Label')]),
    (12, 'Legal', [
        (33, 'Customer Co-ordiantion'), (34, 'International Business'), (35, 'Legal & Complaince')]),
    (37, 'Logistic & Transportation', []),
    (26, 'Marketing & Media', [(75, 'Content Writing'), (74, 'Design & Label'), (73, 'Media')]),
    (42, 'Mayapuri Warehouse', [(98, 'B2C Billing'), (97, 'Packaging & Labling')]),
    (43, 'Media', [(99, 'Design & Label'), (100, 'Media Marketing')]),
    (13, 'PA', [(36, 'Personal Desk')]),
    (20, 'Process', [(45, 'Process')]),
    (33, 'Production', []),
    (27, 'Purchase', [(81, 'EA'), (80, 'Purchase')]),
    (14, 'Purchase & Import', [(37, 'Export'), (38, 'Oil')]),
    (34, 'Purchase & Procurement', []),
    (30, 'Quality', []),
    (15, 'R & A Wing', [(39, 'Business Analytics'), (44, 'Process & Development')]),
    (17, 'Reco', [(41, 'Credit Control')]),
    (36, 'Safety & Securety', []),
    (44, 'Sales', []),
    (39, 'Sales Beverage', [(95, 'GT Sales')]),
    (31, 'Supply Chain', []),
    (18, 'Transport', [(52, 'Dispatch'), (54, 'GRPO'), (77, 'Internal Vehicle'), (42, 'Travelling')]),
    (19, 'Warehouse', [(43, 'Mayapuri Warehouse')]),
]


def _reset_sequences(apps, schema_editor):
    models = [apps.get_model('advance_payment', 'Department'),
              apps.get_model('advance_payment', 'SubDepartment')]
    statements = schema_editor.connection.ops.sequence_reset_sql(no_style(), models)
    with schema_editor.connection.cursor() as cursor:
        for sql in statements:
            cursor.execute(sql)


def load(apps, schema_editor):
    Department = apps.get_model('advance_payment', 'Department')
    SubDepartment = apps.get_model('advance_payment', 'SubDepartment')
    Department.objects.bulk_create(
        [Department(id=dept_id, name=name, is_active=True) for dept_id, name, _subs in DEPARTMENTS])
    SubDepartment.objects.bulk_create([
        SubDepartment(id=sub_id, department_id=dept_id, name=sub_name, is_active=True)
        for dept_id, _name, subs in DEPARTMENTS for sub_id, sub_name in subs
    ])
    _reset_sequences(apps, schema_editor)


def unload(apps, schema_editor):
    SubDepartment = apps.get_model('advance_payment', 'SubDepartment')
    Department = apps.get_model('advance_payment', 'Department')
    SubDepartment.objects.filter(
        id__in=[sub_id for _d, _n, subs in DEPARTMENTS for sub_id, _s in subs]).delete()
    Department.objects.filter(id__in=[dept_id for dept_id, _n, _s in DEPARTMENTS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('advance_payment', '0003_department_sub_department'),
    ]

    operations = [
        migrations.RunPython(load, unload),
    ]
