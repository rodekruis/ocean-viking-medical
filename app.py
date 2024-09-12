import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import os
import json
from dotenv import load_dotenv
from flask import Flask, render_template, request, send_file
from datetime import date
from googleapiclient.discovery import build
from google.oauth2 import service_account
from collections import OrderedDict
import operator

app = Flask(__name__)
load_dotenv()  # take environment variables from .env

referral_states = [
    'Referral is not needed',
    'Referral is needed, not urgent',
    'Referral is needed, urgent',
    'Referral is needed, medevac'
]


def process_data(df_form, bracelet_number=None, first=False):
    """
    process data to show patient information page
    """

    # default page, before inserting bracelet number
    if first:
        return render_template('data.html',
                               first=True)
    if bracelet_number == "":
        return render_template('data.html',
                               no_data=True)

    # error page, in case no data is not found
    if df_form.empty and bracelet_number != "":
        return render_template('data.html',
                               not_found=True,
                               bracelet_number=bracelet_number)

    df = df_form[df_form['bracelet_number'] == bracelet_number]
    df = df.reset_index(drop=True)

    # error page, in case no data is not found
    if df.empty:
        return render_template('data.html',
                               not_found=True,
                               bracelet_number=bracelet_number)

    try:
        df['start'] = pd.to_datetime(df['start'])
    except ValueError:
        df['start'] = pd.to_datetime(df['start'], utc = True)

    # patient info
    info = {}
    for x in ['name', 'gender', 'age']:
        if x in df.columns:
            info_data = df[x].dropna().unique()
            if len(info_data) > 0:
                info[x] = info_data[-1]
            else:
                info[x] = "unknown"
    if 'age' in info.keys():
        info['age'] = map_age(info['age'])

    # consultations and referral status
    consultations = []
    referral = {}
    for ix, row in df.iterrows():
        consultation = {}
        consultation['Date'] = row['start'].date()
        consultation['Diagnosis'] = ""
        for level in ['primary', 'secondary', 'tertiary']:
            case_key = level + '_case'
            # case_label = level.capitalize()+' Diagnosis'
            if case_key in row.keys():
                if not pd.isna(row[case_key]):
                    case = row[case_key]
                    case = case_map(case)
                    if case == "Other":
                        case = row[level + '_case_other']
                    if consultation['Diagnosis'] == "":
                        consultation['Diagnosis'] = case
                    else:
                        consultation['Diagnosis'] = consultation['Diagnosis'] + ", " + case
        if 'history' in row.keys():
            history = row['history']
            if pd.isna(history):
                history = "none"
            consultation['History'] = history
        if 'vital_signs' in row.keys():
            vital_signs = row['vital_signs']
            if pd.isna(vital_signs):
                vital_signs = "none"
            consultation['Vital signs'] = vital_signs
        treatment = row['treatment']
        if pd.isna(treatment):
            treatment = "nothing"
        consultation['Treatment'] = treatment
        if 'info' in row.keys():
            info_text = row['info']
            if not pd.isna(info_text):
                consultation['Other information'] = info_text
        if ix == len(df) - 1:
            if row['referral'] == 'yes':
                if not pd.isna(row['referral_urgency']):
                    urgency = row['referral_urgency'].replace('_', ' ')
                    if urgency != "not needed":
                        referral['Referral is needed:'] = urgency
                    else:
                        referral['Referral is not needed'] = ''
                else:
                    referral['Referral is needed'] = ''
            else:
                referral['Referral is not needed'] = ''
        consultations.append(consultation)

    return render_template('data.html',
                           info=info,
                           consultations=consultations,
                           referral=referral,
                           referral_states=referral_states)


def get_data():
    """
    get data from KoBo
    """
    headers = {'Authorization': f'Token {os.getenv("TOKEN")}'}
    session = requests.Session()
    retry = Retry(connect=10, backoff_factor=0.5)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    data_request = session.get(f'https://kobo.ifrc.org/api/v2/assets/{os.getenv("ASSET")}/data.json',
                               headers=headers)
    data = data_request.json()

    # get rotation info
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
    SAMPLE_SPREADSHEET_ID = os.getenv("GOOGLESHEETID")
    SAMPLE_RANGE_NAME = 'Rotations!A:C'
    sa_file = 'google-service-account-hspatsea-ocean-viking.json'
    creds = service_account.Credentials.from_service_account_file(sa_file, scopes=SCOPES)
    service = build('sheets', 'v4', credentials=creds)
    # Call the Sheets API
    sheet = service.spreadsheets()
    result = sheet.values().get(spreadsheetId=SAMPLE_SPREADSHEET_ID,
                                range=SAMPLE_RANGE_NAME).execute()
    values = result.get('values', [])
    df = pd.DataFrame.from_records(values[1:], columns=values[0])
    df['Start date'] = pd.to_datetime(df['Start date'], dayfirst=True)
    df['End date'] = pd.to_datetime(df['End date'], dayfirst=True)
    df['Rotation No'] = df['Rotation No'].astype(float).round(0).astype(int)
    rotation_no = max(df['Rotation No'])
    start_date_ = pd.to_datetime(date.today())
    end_date_ = pd.to_datetime(date.today())

    if 'results' in data.keys():
        df_form = pd.DataFrame(data['results'])
        if df_form.empty:
            return df_form, rotation_no

        for ix, row in df.iterrows():
            if row['Start date'] <= pd.to_datetime(date.today()) <= row['End date']:
                rotation_no = row['Rotation No']
                start_date_ = pd.to_datetime(row['Start date'], utc=True)
                end_date_ = pd.to_datetime(row['End date'], utc=True)

        df_form['start'] = pd.to_datetime(df_form['start'], utc=True)
        df_form = df_form[(df_form['start'] >= start_date_) & (df_form['start'] <= end_date_)]
        if not df_form.empty:
            df_form['rotation_no'] = rotation_no
        else:
            df_form = pd.DataFrame()
    else:
        df_form = pd.DataFrame()
    return df_form, rotation_no


def process_summary(df_form):
    """
    process data to show summary of morbidities
    """
    morbidities_all, referral_all = {}, {}
    consultations = len(df_form)
    if 'bracelet_number' not in df_form.columns:
        consultations = 0
        patients = 0
        morbidities_all_ordered = {}
        referral_all = {}
    else:
        patients = df_form['bracelet_number'].nunique()
        labels = ['male', 'female', 'u5', 'total']

        for bracelet in df_form['bracelet_number'].unique():
            df = df_form[df_form['bracelet_number'] == bracelet]

            morbidities = {}
            referrals = {}
            for ix, row in df.iterrows():
                for level in ['primary', 'secondary', 'tertiary']:
                    case_key = level + '_case'
                    if case_key in row.keys():
                        if not pd.isna(row[case_key]):
                            case = row[case_key]
                            case = case_map(case)
                            label = ""
                            if case == "Other":
                                case = row[level + '_case_other']
                            if row['age'] == 'u1' or row['age'] == '1_4':
                                label = 'u5'
                            else:
                                if row['gender'] == 'male':
                                    label = 'male'
                                elif row['gender'] == 'female':
                                    label = 'female'
                            if label != "":
                                morbidities[case] = {label: 1, 'total': 1, 'bracelet': bracelet}
                            else:
                                morbidities[case] = {'total': 1, 'bracelet': bracelet}
                if row['referral'] == 'yes':
                    referrals['Referrals needed'] = 1
                    if not pd.isna(row['referral_urgency']):
                        urgency = row['referral_urgency'].replace('_', ' ').capitalize()
                        referrals[urgency] = 1

            for key, morb_dict in morbidities.items():
                if key in morbidities_all.keys():
                    morb_dict_all = morbidities_all[key]
                    for label in labels:
                        if label in morb_dict.keys():
                            morb_dict_all[label] = morb_dict_all[label] + 1
                    morb_dict_all['bracelet'] = morb_dict_all['bracelet'] + ", " + morb_dict["bracelet"]
                    morbidities_all[key] = morb_dict_all
                else:
                    for label in labels:
                        if label not in morb_dict.keys():
                            morb_dict[label] = 0
                    morbidities_all[key] = morb_dict

            for key in referrals.keys():
                if key in referral_all.keys():
                    referral_all[key] = referral_all[key] + 1
                else:
                    referral_all[key] = 1

        # add data of patients without bracelet number
        df_no_bracelet = df_form[pd.isna(df_form['bracelet_number'])].groupby(['age', 'gender']).last().reset_index()
        patients = patients + len(df_no_bracelet)

        for ix, row in df_no_bracelet.iterrows():
            morbidities = {}
            referrals = {}
            for level in ['primary', 'secondary', 'tertiary']:
                case_key = level + '_case'
                if case_key in row.keys():
                    if not pd.isna(row[case_key]):
                        case = row[case_key]
                        case = case_map(case)
                        label = "other"
                        if row['age'] == 'u1' or row['age'] == '1_4':
                            label = 'u5'
                        else:
                            if row['gender'] == 'male':
                                label = 'male'
                            elif row['gender'] == 'female':
                                label = 'female'
                        morbidities[case] = {label: 1, 'total': 1}
            if row['referral'] == 'yes':
                referrals['Referrals needed'] = 1
                if not pd.isna(row['referral_urgency']):
                    urgency = row['referral_urgency'].replace('_', ' ').capitalize()
                    referrals[urgency] = 1

            for key, morb_dict in morbidities.items():
                if key in morbidities_all.keys():
                    morb_dict_all = morbidities_all[key]
                    for label in labels:
                        if label in morb_dict.keys():
                            morb_dict_all[label] = morb_dict_all[label] + 1
                    if 'bracelet' in morb_dict.keys():
                        morb_dict_all['bracelet'] = morb_dict_all['bracelet'] + ", " + morb_dict["bracelet"]
                    else:
                        morb_dict_all['bracelet'] = morb_dict_all['bracelet'] + ", " + 'NaN'
                    morbidities_all[key] = morb_dict_all
                else:
                    for label in labels:
                        if label not in morb_dict.keys():
                            morb_dict[label] = 0
                    morbidities_all[key] = morb_dict

            for key in referrals.keys():
                if key in referral_all.keys():
                    referral_all[key] = referral_all[key] + 1
                else:
                    referral_all[key] = 1

        # sort morbidities by total
        morbidities_all_ordered = OrderedDict()
        totals = {}
        for key, value in morbidities_all.items():
            totals[key] = value['total']
        sorted_totals = dict(sorted(totals.items(), key=operator.itemgetter(1), reverse=True))
        for key in sorted_totals.keys():
            morbidities_all_ordered[key] = morbidities_all[key]

    return render_template('summary.html',
                           consultations=consultations,
                           patients=patients,
                           morbidities=morbidities_all_ordered,
                           referrals=referral_all)


@app.route("/updatesubmission", methods=['POST'])
def update_submission():
    """
    update referral status
    """
    bracelet_number = request.form['bracelet']
    df_form, rotation_no = get_data()
    df = df_form[df_form['bracelet_number'] == bracelet_number].reset_index(drop=True)

    if df.empty:
        return process_data(df_form, bracelet_number)

    submission_id = df.iloc[len(df) - 1]['_id']

    # update submission in kobo
    url = f'https://kobo.ifrc.org/api/v2/assets/{os.getenv("ASSET")}/data/bulk/'
    headers = {'Authorization': f'Token {os.getenv("TOKEN")}'}
    params = {'format': 'json'}

    referral_update = request.form['referral']

    if referral_update == 'Referral is not needed':
        payload = {
            "submission_ids": [str(submission_id)],
            "data": {"referral": "no"}
        }
        requests.patch(
            url=url,
            data={'payload': json.dumps(payload)},
            params=params,
            headers=headers
        )
    else:
        payload = {
            "submission_ids": [str(submission_id)],
            "data": {"referral": "yes"}
        }
        requests.patch(
            url=url,
            data={'payload': json.dumps(payload)},
            params=params,
            headers=headers
        )
        referral_urgency_dict = {
            'Referral is needed, not urgent': 'not_urgent',
            'Referral is needed, urgent': 'urgent',
            'Referral is needed, medevac': 'medevac'
        }
        payload = {
            "submission_ids": [str(submission_id)],
            "data": {"referral_urgency": referral_urgency_dict[referral_update]}
        }
        requests.patch(
            url=url,
            data={'payload': json.dumps(payload)},
            params=params,
            headers=headers
        )

    df_form, rotation_no = get_data()
    return process_data(df_form, bracelet_number)


@app.route("/downloaddata", methods=['POST'])
def download_data():
    df_form, rotation_no = get_data()

    # keep only referrals
    df_form = df_form[df_form['referral'] == 'yes']

    # change age
    df_form['age'] = df_form['age'].apply(map_age)

    # merge diagnoses
    df_form['diagnosis'] = df_form['primary_case']
    for ix, row in df_form.iterrows():
        diagnosis = case_map(row['diagnosis'])
        if 'secondary_case' in df_form.columns:
            if not pd.isna(row['secondary_case']):
                diagnosis = diagnosis + ", " + case_map(row['secondary_case'])
        if 'tertiary_case' in df_form.columns:
            if not pd.isna(row['tertiary_case']):
                diagnosis = diagnosis + ", " + case_map(row['tertiary_case'])
        df_form.at[ix, 'diagnosis'] = diagnosis
    df_form = df_form.drop(columns=['primary_case', 'secondary_case', 'tertiary_case'])

    # keep only meaningful columns
    columns_to_keep = ['_submission_time',
                       'bracelet_number',
                       'name',
                       'gender',
                       'age',
                       'diagnosis',
                       'history',
                       'vital_signs',
                       'treatment',
                       'info',
                       'referral_urgency']
    columns_to_keep = [x for x in columns_to_keep if x in df_form.columns]
    df_form = df_form[columns_to_keep]

    # order by bracelet number
    df_form['bracelet_number'] = df_form['bracelet_number'].astype(float, errors='ignore')
    df_form = df_form.sort_values(by='bracelet_number')

    data_path = 'referral-data.xlsx'
    if os.path.exists(data_path):
        os.remove(data_path)
    writer = pd.ExcelWriter(data_path, engine='xlsxwriter')
    df_form.to_excel(writer, sheet_name='DATA', index=False)  # send df to writer
    worksheet = writer.sheets['DATA']  # pull worksheet object
    for idx, col in enumerate(df_form.columns):  # loop through all columns
        series = df_form[col]
        max_len = max((
            series.astype(str).map(len).max(),  # len of largest item
            len(str(series.name))  # len of column name/header
        )) + 1
        worksheet.set_column(idx, idx, max_len)  # set column width
    writer.save()
    return send_file(data_path, as_attachment=True)


@app.route("/data", methods=['POST'])
def default_page():
    """
    show patient information
    """
    if request.form['password'] == os.getenv("PASSWORD"):
        df_form, rotation_no = get_data()
        return process_data(df_form, first=True)
    else:
        return render_template('home.html')


@app.route("/dataupdate", methods=['POST'])
def update_bracelet():
    """
    get bracelet number and show patient info
    """
    if 'bracelet' in request.form.keys():
        bracelet_number = request.form['bracelet']
    else:
        bracelet_number = None
    df_form, rotation_no = get_data()
    return process_data(df_form, bracelet_number)


@app.route("/summary", methods=['POST'])
def summary():
    """
    show summary of morbidities
    """
    df_form, rotation_no = get_data()
    return process_summary(df_form)


@app.route("/")
def login_page():
    """
    login page
    """
    return render_template('home.html')


def case_map(case):
    """
    map KoBo XLS column names to human-readable format
    """
    case_map_dict = {'male': 'Male', 'female': 'Female', 'other': 'Other', 'u1': 'Less than 1 year', '1_4': '1-4 years',
                     '5_17': '5-17 years', '18_50': '18-50 years', '50p': '50+ years', 'scabies': 'Scabies',
                     'sea_sickness': 'Sea sickness', 'herpes': 'Herpes lip / cold sore', 'skin': 'Other skin condition',
                     'gastritis': 'Gastritis', 'dental': 'Dental', 'injury': 'Non-violence related injury',
                     'violence': 'Violence related injury', 'fuel_burn': 'Fuel burns',
                     'exposure_skin': 'Exposure related skin disorder', 'dehydration': 'Dehydration',
                     'hypothermia': 'Hypothermia', 'body_pain': 'Generalized body pain / headache',
                     'awd': 'Acute watery diarrhoea', 'nicotine': 'Nicotine withdrawal',
                     'sawd': 'Severe acute diarrhoea', 'abd': 'Acute bloody diarrhoea',
                     'chronic_diarrhoea': 'Chronic diarrhoea', 'const': 'Constipation',
                     'fever': 'Fever without identified cause', 'urti': 'Acute upper respiratory tract infection / common cold',
                     'lrti': 'Acute lower respiratory tract infection', 'tb': 'Tuberculosis (suspected)',
                     'meningitis': 'Meningitis (suspected)', 'std': 'Sexually transmitted infection (suspected)',
                     'uti': 'Urinary tract infection', 'eye': 'Eye infection', 'gyno': 'Gynaecological disorder',
                     'anaemia': 'Anaemia', 'malnutrition': 'Severe acute malnutrition', 'chronic': 'Chronic disease',
                     'cpd': 'Mental health presentation', 'spd': 'Severe psychiatric disorder',
                     'covid': 'Confirmed COVID-19', 'sv': 'SV/SGBV', 'pregnancy': 'Pregnancy related (ANC & PNC)',
                     'pregnancy_anc': 'Pregnancy ANC', 'pregnancy_pnc': 'Pregnancy PNC',
                     'baby': 'Baby consultation', 'yes': 'yes', 'no': 'no'}
    if case in case_map_dict.keys():
        return case_map_dict[case]
    else:
        return case


def map_age(age):
    """
    map KoBo XLS column names to human-readable format
    """
    age_dict = {'u1': 'less than 1 year',
                '1_4': '1-4 years',
                '5_12': '5-12 years',
                '13_17': '13-17 years',
                '18_50': '18-50 years',
                '50p': '50+ years'}
    if age in age_dict.keys():
        return age_dict[age]
    else:
        return age
