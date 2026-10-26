<img width="2560" height="1700" alt="image" src="https://github.com/user-attachments/assets/b2ce08b3-f919-4e3c-b66c-6fd7a0f0f556" />

# Hugh-Speak
espeak-ng style voice synthesis for Granite Hugh.

https://github.com/behelit2/Granite-Hugh

# Step 1, Install packages:
```
sudo dnf install python3 python3-prompt-toolkit espeak-ng bash openssh-clients
```

# Step 2, place hugh_speak.py in your home directory.

# Step 3, configure for your setup.

Hugh can run locally or on a remote server, and this script accommodates both configurations. 

Edit hugh_speak.py to change the configuration settings.

Set your CONNECTION_MODE to local or ssh. Make sure your local or SSH settings are correct, perspectively. You can change the VOICE, PITCH, and WORDS_PER_MINUTE in the Local voice settings.

# Step 4, Run.

```
python3 hugh_speak.py
```

Depending on whether you picked local or ssh, Hugh will start right away or you will be prompted for your SSH password (after which Hugh starts right away). Starting to type interrupts the voice.

*NOTE - The Granite Hugh interface will work slightly differently due to how the wrapper works. CTRL+C will not remove previous turns and will exit. And it will not remember your normal prompt history, starting a fresh history each time.
