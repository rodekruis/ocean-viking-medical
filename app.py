import requests
import pandas as pd
from collections import OrderedDict
import io
import os
import json
from dotenv import load_dotenv
# from azure.storage.blob import BlobServiceClient
from flask import Flask, render_template, request, send_file
app = Flask(__name__)
load_dotenv()  # take environment variables from .env

referral_states = [
    'Referral is not needed',
    'Referral is needed, not urgent',
    'Referral is needed, urgent',
    'Referral is needed, medevac'
]


def process_data(df_form, bracelet_number=None, first=False):

    if first:
        return render_template('data.html',
                               first=True)
    if bracelet_number == "":
        return render_template('data.html',
                               no_data=True)
    if df_form.empty and bracelet_number != "":
        return render_template('data.html',
                               not_found=True,
                               bracelet_number=bracelet_number)

    df = df_form[df_form['bracelet_number'] == bracelet_number]
    df = df.reset_index(drop=True)

    if df.empty:
        return render_template('data.html',
                               not_found=True,
                               bracelet_number=bracelet_number)

    df['start'] = pd.to_datetime(df['start'])
    df['date'] = df['start'].dt.date

    # generic info
    info = {}
    for x in ['name', 'gender', 'age']:
        if x in df.columns:
            info[x] = df[x].unique()[-1]
    if 'age' in info.keys():
        info['age'] = map_age(info['age'])

    # consultations
    consultations = []
    referral = {}
    for ix, row in df.iterrows():
        consultation = {}
        consultation['Date'] = row['date']
        for level in ['primary', 'secondary', 'tertiary']:
            case_key = level+'_case'
            case_label = level.capitalize()+' Diagnosis'
            if case_key in row.keys():
                if not pd.isna(row[case_key]):
                    case = row[case_key]
                    case = case_map(case)
                    consultation[case_label] = case
        treatment = row['treatment']
        if pd.isna(treatment):
            treatment = "nothing"
        consultation['Treatment'] = treatment
        if ix == len(df)-1:
            if row['referral'] == 'yes':
                referral['Referral is needed:'] = row['referral_urgency']
            else:
                referral['Referral is not needed'] = ''
        consultations.append(consultation)

    return render_template('data.html',
                           info=info,
                           consultations=consultations,
                           referral=referral,
                           referral_states=referral_states)


def get_data():
    # get data from kobo
    headers = {'Authorization': f'Token {os.getenv("TOKEN")}'}
    data_request = requests.get(f'https://kobonew.ifrc.org/api/v2/assets/{os.getenv("ASSET")}/data.json', headers=headers)
    data = data_request.json()
    if 'results' in data.keys():
        df_form = pd.DataFrame(data['results'])
    else:
        df_form = pd.DataFrame()
    return df_form


def process_summary(df_form):

    morbidities_all, referral_all = {}, {}
    patients = df_form['bracelet_number'].nunique()

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
                        morbidities[case] = 1
            if row['referral'] == 'yes':
                referrals['Referrals needed'] = 1
                urgency = row['referral_urgency'].replace('_', ' ').capitalize()
                referrals[urgency] = 1

        for key in morbidities.keys():
            if key in morbidities_all.keys():
                morbidities_all[key] = morbidities_all[key] + 1
            else:
                morbidities_all[key] = 1
        for key in referrals.keys():
            if key in referral_all.keys():
                referral_all[key] = referral_all[key] + 1
            else:
                referral_all[key] = 1

    df_no_bracelet = df_form[pd.isna(df_form['bracelet_number'])].groupby(['age', 'gender']).last()
    patients = patients + len(df_no_bracelet)

    morbidities = {}
    referrals = {}
    for ix, row in df_no_bracelet.iterrows():
        for level in ['primary', 'secondary', 'tertiary']:
            case_key = level + '_case'
            if case_key in row.keys():
                if not pd.isna(row[case_key]):
                    case = row[case_key]
                    case = case_map(case)
                    morbidities[case] = 1
        if row['referral'] == 'yes':
            referrals['Referrals needed'] = 1
            urgency = row['referral_urgency'].replace('_', ' ').capitalize()
            referrals[urgency] = 1

    for key in morbidities.keys():
        if key in morbidities_all.keys():
            morbidities_all[key] = morbidities_all[key] + 1
        else:
            morbidities_all[key] = 1
    for key in referrals.keys():
        if key in referral_all.keys():
            referral_all[key] = referral_all[key] + 1
        else:
            referral_all[key] = 1

    return render_template('summary.html',
                           patients=patients,
                           morbidities=morbidities_all,
                           referrals=referral_all)


@app.route("/updatesubmission", methods=['POST'])
def update_submission():

    bracelet_number = request.form['bracelet']
    df_form = get_data()
    df = df_form[df_form['bracelet_number'] == bracelet_number].reset_index(drop=True)

    if df.empty:
        return process_data(df_form, bracelet_number)

    submission_id = df.iloc[len(df)-1]['_id']

    # update submission in kobo
    url = f'https://kobonew.ifrc.org/api/v2/assets/{os.getenv("ASSET")}/data/bulk/'
    headers = {'Authorization': f'Token {os.getenv("TOKEN")}'}
    params = {'fomat': 'json'}

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

    df_form = get_data()
    return process_data(df_form, bracelet_number)


@app.route("/data", methods=['POST'])
def default_page():
    if request.form['password'] == os.getenv("PASSWORD"):
        df_form = get_data()
        return process_data(df_form, first=True)
    else:
        return render_template('home.html')


@app.route("/dataupdate", methods=['POST'])
def update_bracelet():
    if 'bracelet' in request.form.keys():
        bracelet_number = request.form['bracelet']
    else:
        bracelet_number = None
    df_form = get_data()
    return process_data(df_form, bracelet_number)


@app.route("/summary", methods=['POST'])
def summary():
    df_form = get_data()
    return process_summary(df_form)


@app.route("/")
def login_page():
    return render_template('home.html')


def case_map(case):
    case_map_dict = {'male': 'Male', 'female': 'Female', 'other': 'Other', 'u1': 'Less than 1 year', '1_4': '1-4 years',
                     '5_17': '5-17 years', '18_50': '18-50 years', '50p': '50+ years', 'scabies': 'Scabies',
                     'sea_sickness': 'Sea sickness', 'herpes': 'Herpes lip / cold sore', 'skin': 'Other skin infection',
                     'gastritis': 'Gastritis', 'dental': 'Dental', 'injury': 'Non-violence related injury',
                     'violence': 'Violence related injury', 'fuel_burn': 'Fuel Burns',
                     'exposure_skin': 'Exposure related skin disorder', 'dehydration': 'Dehydration',
                     'hypothermia': 'Hypothermia', 'body_pain': 'Generalized body pain / headache', 'awd': 'Acute watery diarrhoea',
                     'sawd': 'Severe acute diarrhoea', 'abd': 'Acute bloody diarrhoea',
                     'fever': 'Fever without identified cause', 'urti': 'Acute upper respiratory tract infection',
                     'lrti': 'Acute lower respiratory tract infection', 'tb': 'Tuberculosis (suspected)',
                     'meningitis': 'Meningitis (suspected)', 'std': 'Sexually transmitted infection (suspected)',
                     'uti': 'Urinary tract infection', 'eye': 'Eye infection', 'gyno': 'Gynaecological disease',
                     'anaemia': 'Anaemia', 'malnutrition': 'Severe acute malnutrition', 'chronic': 'Chronic disease',
                     'cpd': 'Mental health presentation', 'spd': 'Severe psychiatric disorder',
                     'covid': 'Confirmed COVID-19', 'sv': 'SV/SGBV', 'pregnancy': 'Pregnancy related (ANC & PNC)',
                     'baby': 'Baby consultation', 'yes': 'yes', 'no': 'no'}
    if case in case_map_dict.keys():
        return case_map_dict[case]
    else:
        return case


def map_age(age):
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


