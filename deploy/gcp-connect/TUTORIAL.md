# Connect a Google Cloud project to Valxel

<walkthrough-tutorial-duration duration="5"></walkthrough-tutorial-duration>

This makes two service accounts in your project that Valxel can act as, and nothing else of
Valxel's: `valxel-deploy` (what your products need, scoped to the mode you chose) and
`valxel-agents` (read-only). Valxel never gets a key: it acts as them for 15 minutes at a time.

## Run the template

Paste the command from Valxel's connections page in the terminal below, with your project ID in
place of `YOUR_PROJECT_ID`. It looks like:

```sh
terraform init && terraform apply -var project=YOUR_PROJECT_ID -var workspace=... -var connect_label=... -var mode=lite -var region=us-central1 -var valxel_service_account=...
```

Read the plan, then type `yes`. It takes a minute or two.

## Turn on the billing export

Valxel's cost reviews read the Cloud Billing export. Google has no API to turn it on, so a
billing account administrator does it once: **Billing**, **Billing export**, **BigQuery export**,
**Standard usage cost**, then this project and the `valxel_billing` dataset. Do it in one project
per billing account (`-var billing_dataset=false` in the others).

## Check the connection

Back on Valxel's connections page, enter your project ID and choose **Check the connection**.

To disconnect later: `terraform destroy` here, or delete the `valxel-*` service accounts.
