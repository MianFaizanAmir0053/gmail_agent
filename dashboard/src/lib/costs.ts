/**
 * The Costs page's totals. No framework imports, so `node --test` runs them;
 * what the SQL counts is tested on Postgres (tests/test_dashboard_costs.py).
 *
 * Since M19 a chat turn is a run too, metered under `chat-<turn>`. It counts
 * toward the spend, but it is not an email, so the cost per hundred emails
 * leaves it out. One email can be several runs (a start, a resume, a redrive),
 * so the emails are counted, not the runs.
 */

/** A run of the mail pipeline: anything but a chat turn. */
export const MAIL_RUN = "gmail_message_id NOT LIKE 'chat-%'";

/** The last 30 days: every run's spend and count, and the mail runs' spend
 * and the emails they ran for. */
export const TOTALS_SQL = `
  SELECT sum(total_cost_usd) AS cost, count(*) AS runs,
         sum(total_cost_usd) FILTER (WHERE ${MAIL_RUN}) AS mail_cost,
         count(DISTINCT gmail_message_id) FILTER (WHERE ${MAIL_RUN}) AS mail_emails
    FROM runs WHERE started_at > now() - interval '30 days'
`;

/** What a hundred emails cost, from the mail runs alone; null with none. */
export function perHundredEmails(mailCost: number, emails: number): number | null {
  return emails > 0 ? (mailCost / emails) * 100 : null;
}
