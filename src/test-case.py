import requests

c = 1
for i in range(100):
    try:
        response = requests.get("https://github.com")
        print("Request "+str(c)+" sent successfully!")
    except requests.exceptions.RequestException as e:
        print("An error occurred:", e)
    c+=1