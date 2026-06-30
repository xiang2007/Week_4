from backend.src.app import calculate_tax

def main():
    try:
        while True:
            i = int(input("Enter your income: "))
            print(f"You must pay {calculate_tax(i):.2f} amount of tax")
    except KeyboardInterrupt:
        print("Exit")


if __name__ == "__main__":
    main()
