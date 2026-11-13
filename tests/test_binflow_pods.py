import csv
import io
import re
import sqlite3
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash
import test_binflow as legacy


class DailyPODTests(unittest.TestCase):
    setUp = legacy.BinFlowTests.setUp
    tearDown = legacy.BinFlowTests.tearDown
    post = legacy.BinFlowTests.post
    rows = legacy.BinFlowTests.rows
    login = legacy.BinFlowTests.login
    job = legacy.BinFlowTests.job
    def initial_delivery(self):
        legacy.BinFlowTests.initial_delivery(self)
        from pod_helpers import sign_pod
        sign_pod(self,1)
    exchange = legacy.BinFlowTests.exchange

    def file(self, action, version=None, follow=True):
        if version is None: version = self.rows('SELECT pod_version FROM bf_jobs WHERE id=1')[0]['pod_version']
        return self.post('/binflow/pods/1/file', dict(action=action, version=version), follow=follow)

    def email_form(self):
        page = self.client.get('/binflow/pods/1/email')
        self.assertEqual(page.status_code, 200)
        return dict(request_key=re.search(r'name="request_key" value="([^"]+)"', page.text).group(1),
                    version=self.rows('SELECT pod_version FROM bf_jobs WHERE id=1')[0]['pod_version'],
                    recipient='recipient@example.com', message='Please find your POD attached.')

    def client_portal(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("""INSERT INTO bf_client_accounts(id,username,name,password_hash,created_at,updated_at,updated_by)
                VALUES(1,'client','Client','unused','2026-10-19','2026-10-19','Admin')""")
            conn.execute('INSERT INTO bf_client_account_scope VALUES(1,1)')
        client=self.app.test_client()
        with client.session_transaction() as s:s['bf_client_uid']=1;s['bf_client_version']=1
        return client

    def test_single_bin_next_and_read_only_receipt(self):
        self.initial_delivery()
        self.job(collect=(1,2))
        self.exchange(2,(1,2))
        self.post('/binflow/pickups/2/receive',dict(confirmed=[1,2]))
        first,second=self.rows("SELECT id,bin_code FROM bf_job_bins WHERE job_id=2 AND direction='collect' ORDER BY bin_code")
        def url(row):
            return '/binflow/pickups/2/weigh/'+str(row['id'])
        form=dict(gross='55',tare='5',confirm_tare='yes',return_to='bin')
        self.assertEqual(self.client.get('/binflow/pickups/2').text.count('name="gross"'),1)
        saved=self.post(url(first),form)
        self.assertEqual(saved.request.path,url(first))
        self.assertIn('Weight saved · Locked',saved.text)
        self.assertIn('Next bin · '+second['bin_code'],saved.text)
        self.assertNotIn('name="gross"',saved.text)
        self.assertNotIn(url(first)+'#weigh-',self.client.get('/binflow/pods').text)
        self.assertIn('cannot be overwritten',self.post(url(first),dict(form,gross='99')).text)
        self.assertEqual(self.rows('SELECT gross_g FROM bf_job_bins WHERE id=?',(first['id'],))[0]['gross_g'],55000)
        self.assertEqual(self.client.get(url(second)).text.count('name="gross"'),1)
        self.assertIn('No more bins',self.post(url(second),form).text)

    def test_returns_survive_date_changes_and_week_closes_only_after_signature(self):
        from pod_helpers import sign_pod
        self.initial_delivery()
        self.job(collect=(1,2))
        self.exchange(2,(1,2))
        first,second=self.rows('SELECT id,bin_id FROM bf_job_bins WHERE job_id=2 ORDER BY bin_id')
        with sqlite3.connect(self.path) as conn:
            conn.execute("""UPDATE bf_jobs SET scan_mode=1,collected_at='2026-10-16T13:00:00+10:00'
                WHERE id=2""")
            conn.execute("UPDATE bf_job_bins SET returned_at='2026-10-16T17:00:00+10:00' WHERE id=?", (first['id'],))
            conn.execute("UPDATE bf_bins SET status='received',location_id=1 WHERE id=1")
        url='/binflow/pods?date=2026-10-12'
        page=self.client.get(url).text
        self.assertIn('/binflow/bins/1/qr.png',page)
        self.assertNotIn('/binflow/bins/2/qr.png',page)
        self.assertIn('2d 15h waiting',page)
        self.assertIn('1 overdue',page)
        self.assertIn('0 / 2',page)
        form=dict(gross=15,tare=5,confirm_tare='yes',return_to='pods',date='2026-10-12')
        saved=self.post(f"/binflow/pickups/2/weigh/{first['id']}",form,follow=False)
        self.assertIn(url+'#pending-weigh-ins',saved.location)
        page=self.client.get(url).text
        self.assertNotIn('/binflow/bins/1/qr.png',page)
        self.assertIn('1 / 2',page)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_jobs SET status='received',received_at='2026-10-19T08:00:00+10:00' WHERE id=2")
            conn.execute("UPDATE bf_job_bins SET returned_at='2026-10-19T08:00:00+10:00' WHERE id=?",(second['id'],))
            conn.execute("UPDATE bf_bins SET status='received',location_id=1 WHERE id=2")
        self.post(f"/binflow/pickups/2/weigh/{second['id']}",form)
        page=self.client.get(url).text
        self.assertIn('2 / 2',page)
        self.assertIn('1 overdue',page)
        self.assertIn('Check run &amp; sign off',page)
        self.assertEqual(self.client_portal().get('/binflow/client/pods/2.pdf').status_code,404)
        sign_pod(self,2)
        page=self.client.get(url).text
        self.assertNotIn('1 overdue',page)
        self.assertIn('1 signed after week-end',page)
        self.assertIn('1 / 1',page)

    def test_queue_groups_legacy_receipts_and_limits_warehouse_qrs(self):
        self.initial_delivery()
        self.job(collect=(1,))
        self.exchange(2,(1,))
        self.post('/binflow/pickups/2/receive',dict(confirmed=[1]))
        self.post('/binflow/bins',dict(warehouse_id=2,size_id=1,quantity=1,tare=5))
        self.job(deliver=(7,),site=5,warehouse=2)
        self.exchange(3,(7,))
        self.job(collect=(7,),site=5,warehouse=2)
        self.exchange(4,(7,))
        self.post('/binflow/pickups/4/receive',dict(confirmed=[7]))
        url='/binflow/pods?date=2026-10-22'
        page=self.client.get(url).text
        for value in ('<h3>NSW','<h3>QLD','/binflow/bins/1/qr.png','/binflow/bins/7/qr.png','2 bins'):
            self.assertIn(value,page)
        with sqlite3.connect(self.path) as conn:
            uid=conn.execute("""INSERT INTO users(role,first_name,last_name,email,username,password_hash,extra_business_roles,working_state)
                VALUES('employee','Warehouse','User','wh@example.com','wh',?,'warehouse','QLD')""",
                (generate_password_hash('test-admin-password'),)).lastrowid
        self.post('/logout');self.login('employee','wh')
        page=self.client.get(url).text
        self.assertIn('/binflow/bins/1/qr.png',page)
        self.assertNotIn('/binflow/bins/7/qr.png',page)
        self.assertEqual(self.client.get('/binflow/bins/1/qr.png').mimetype,'image/png')
        self.assertEqual(self.client.get('/binflow/bins/7/qr.png').status_code,404)
        self.assertEqual(self.client.get('/binflow/bins/2/qr.png').status_code,404)
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT INTO bf_bale_managers(user_id,warehouse_id) VALUES(?,2)',(uid,))
        self.assertNotIn('/binflow/bins/1/qr.png',self.client.get(url).text)
        self.assertEqual(self.client.get('/binflow/bins/1/qr.png').status_code,404)

    def test_filing_preserves_stock_history_and_client_visibility(self):
        self.initial_delivery(); client=self.client_portal()
        before={table:self.rows('SELECT * FROM '+table) for table in ('bf_bins','bf_job_bins','bf_events')}
        job=self.rows('SELECT * FROM bf_jobs')[0]
        self.assertEqual(job['status'],'complete')
        page=self.client.get('/binflow/pods?date=2026-10-19').text
        for text in ('Archive','Delete','Email','Export PDF','Export list CSV'):self.assertIn(text,page)
        self.file('archive')
        self.assertNotIn('BF-000001',self.client.get('/binflow/pods?date=2026-10-19').text)
        self.assertIn('BF-000001',self.client.get('/binflow/pods?date=2026-10-19&view=archived').text)
        self.assertIn('BF-000001',client.get('/binflow/client/pods?date=2026-10-19').text)
        self.assertEqual(client.get('/binflow/client/pods/1.pdf').status_code,200)
        self.file('delete')
        self.assertIn('BF-000001',self.client.get('/binflow/pods?date=2026-10-19&view=deleted').text)
        self.assertNotIn('BF-000001',client.get('/binflow/client/pods?date=2026-10-19').text)
        self.assertEqual(client.get('/binflow/client/pods/1.pdf').status_code,404)
        for path in ('/binflow/pickups/1/pod','/binflow/pickups/1/pod.pdf','/binflow/pods/1/email'):
            self.assertEqual(self.client.get(path).status_code,410,path)
        self.assertEqual(self.client.get('/binflow/pickups/1').status_code,200)
        self.file('restore')
        self.assertIn('BF-000001',self.client.get('/binflow/pods?date=2026-10-19').text)
        self.assertEqual(client.get('/binflow/client/pods/1.pdf').status_code,200)
        after=self.rows('SELECT * FROM bf_jobs')[0]
        for key,value in job.items():
            if key!='pod_version':self.assertEqual(after[key],value,key)
        for table,value in before.items():self.assertEqual(self.rows('SELECT * FROM '+table),value)
        self.assertEqual([h['action'] for h in self.rows('SELECT * FROM bf_pod_history')],['Warehouse weights checked and POD signed off','archive','delete','restore'])

    def test_only_completed_pods_and_admin_with_csrf_can_be_filed(self):
        self.job(deliver=(1,2))
        self.assertIn('Finish the handover',self.file('delete').text)
        self.assertIsNone(self.rows('SELECT pod_deleted_at FROM bf_jobs')[0]['pod_deleted_at'])
        self.assertEqual(self.client.get('/binflow/pods/1/email').status_code,400)
        self.exchange(1,(1,2))
        from pod_helpers import sign_pod
        sign_pod(self,1)
        self.assertEqual(self.client.post('/binflow/pods/1/file',data=dict(action='delete',version=0)).status_code,400)
        self.file('archive')
        self.assertIn('This POD changed',self.file('delete',0).text)
        self.assertIsNone(self.rows('SELECT pod_deleted_at FROM bf_jobs')[0]['pod_deleted_at'])
        with sqlite3.connect(self.path) as conn:
            uid=conn.execute("""INSERT INTO users(role,first_name,email,username,password_hash,business_role,working_state)
                VALUES('employee','Driver','driver@example.com','driver',?,'driver','QLD')""",
                (generate_password_hash('test-admin-password'),)).lastrowid
            conn.execute('UPDATE bf_jobs SET driver_id=?',(uid,))
        self.post('/logout');self.login('employee','driver')
        for path in ('/binflow/pods/1/email','/binflow/pods/export.csv?date=2026-10-19','/binflow/pods?view=deleted','/binflow/pods?view=archived'):
            self.assertEqual(self.client.get(path).status_code,403,path)
        self.assertEqual(self.file('delete').status_code,403)
        self.assertEqual(self.post('/binflow/pods/1/email',{}).status_code,403)

    def test_csv_respects_date_filing_and_neutralises_formula_values(self):
        self.initial_delivery()
        with sqlite3.connect(self.path) as conn:conn.execute("UPDATE bf_jobs SET site_name='=HYPERLINK(\"bad\")' WHERE id=1")
        response=self.client.get('/binflow/pods/export.csv?date=2026-10-19')
        data=list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertEqual(len(data),2);self.assertEqual(data[1][0],'BF-000001');self.assertTrue(data[1][3].startswith("'="))
        self.assertIn('PODs-2026-10-19-active.csv',response.headers['Content-Disposition'])
        self.file('archive')
        self.assertNotIn(b'BF-000001',self.client.get('/binflow/pods/export.csv?date=2026-10-19').data)
        self.assertIn(b'BF-000001',self.client.get('/binflow/pods/export.csv?date=2026-10-19&view=archived').data)
        self.assertNotIn(b'BF-000001',self.client.get('/binflow/pods/export.csv?date=2026-10-20&view=archived').data)
        self.assertEqual(self.client.get('/binflow/pods/export.csv?view=invalid').status_code,400)

    def test_email_attaches_actual_pdf_records_delivery_and_deduplicates(self):
        self.initial_delivery()
        self.app.config.update(SMTP_HOST='smtp.example.com',SMTP_FROM='ops@example.com',SMTP_USERNAME='ops',SMTP_PASSWORD='test')
        values=self.email_form()
        with patch('binflow_pods.smtplib.SMTP') as smtp:
            response=self.post('/binflow/pods/1/email',values)
            self.assertIn('POD emailed to recipient@example.com.',response.text)
            server=smtp.return_value.__enter__.return_value
            server.starttls.assert_called_once();server.login.assert_called_once_with('ops','test')
            msg=server.send_message.call_args.args[0]
            self.assertEqual(msg['To'],'recipient@example.com')
            attachment=next(msg.iter_attachments())
            self.assertEqual(attachment.get_filename(),'BF-000001-POD.pdf')
            self.assertEqual(attachment.get_content_type(),'application/pdf')
            self.assertTrue(attachment.get_payload(decode=True).startswith(b'%PDF-'))
            self.assertIn('already been sent',self.post('/binflow/pods/1/email',values).text)
            server.send_message.assert_called_once()
        saved=self.rows('SELECT * FROM bf_pod_emails')
        self.assertEqual(len(saved),1);self.assertEqual(saved[0]['status'],'sent');self.assertTrue(saved[0]['sent_at'])

    def test_email_validation_config_failure_and_stale_form_never_claim_success(self):
        self.initial_delivery();self.app.config.update(SMTP_HOST='',SMTP_FROM='')
        values=self.email_form()
        self.assertIn('Email is not connected',self.post('/binflow/pods/1/email',values).text)
        self.app.config.update(SMTP_HOST='smtp.example.com',SMTP_FROM='ops@example.com')
        with patch('binflow_pods.smtplib.SMTP') as smtp:
            for recipient in ('bad','a@example.com,b@example.com','a@example.com\r\nBcc: b@example.com'):
                self.assertEqual(self.post('/binflow/pods/1/email',{**values,'recipient':recipient}).status_code,400)
            self.assertEqual(self.client.post('/binflow/pods/1/email',data=values).status_code,400)
            self.assertEqual(self.post('/binflow/pods/1/email',{**values,'request_key':'wrong'}).status_code,400)
            self.file('archive')
            self.assertEqual(self.post('/binflow/pods/1/email',values).status_code,409)
            smtp.assert_not_called()
        self.assertEqual(self.rows('SELECT * FROM bf_pod_emails'),[])
        values=self.email_form()
        with patch('binflow_pods.smtplib.SMTP',side_effect=OSError('offline')):
            response=self.post('/binflow/pods/1/email',values)
        self.assertEqual(response.status_code,502);self.assertIn('could not be emailed',response.text)
        row=self.rows('SELECT * FROM bf_pod_emails')[0];self.assertEqual(row['status'],'failed');self.assertIsNone(row['sent_at'])
        with patch('binflow_pods.smtplib.SMTP') as smtp:
            self.assertEqual(self.post('/binflow/pods/1/email',values).status_code,409);smtp.assert_not_called()


if __name__=='__main__':unittest.main()
