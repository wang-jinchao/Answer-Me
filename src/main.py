from config import (
    load_accounts,
    get_url
)
from account_runner import run_account
from report import save
def main():
    accounts=load_accounts()
    url=get_url()
    results=[]
    for account in accounts:
        print(
            "Running:",
            account["name"]
        )
        result=run_account(
            account,
            url
        )
        results.append(result)
    save(results)
    
if __name__=="__main__":
    main()