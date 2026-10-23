import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from mocap_recorder.app import main
    main()
