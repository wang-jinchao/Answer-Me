from browser import Browser



def run_account(account,url):


    result={

        "user":
            account["name"],

        "status":
            "failed"

    }


    browser=Browser()


    try:


        page=browser.start()


        page.goto(url)


        # TODO:
        # 添加你有权限系统的自动化步骤


        result["status"]="success"



    except Exception as e:


        result["error"]=str(e)



    finally:

        browser.close()



    return result