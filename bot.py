import os
from telegram.ext import ExtBot, Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, Defaults
from telegram.utils.request import Request
import sys
import logging
import traceback
import atexit
from dotenv import load_dotenv
import shutil
from datetime import datetime
import fcntl
import time
import csv
import urllib3
from telegram import Bot
from telegram.ext import Defaults
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
    CallbackContext
)
from credit_system import (
    handle_credit_buttons,  # mantieni solo quelle che usi
    admin_check_orders,
    admin_confirm_payment
)

import logging
logging.basicConfig(
    filename='/home/InstantSong/bot.log',
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

class Config:
    BASE_PATH = '/home/InstantSong'
    DEMO_PATH = os.path.join(BASE_PATH, 'demo')
    BACKUP_PATH = os.path.join(BASE_PATH, 'backup')
    LOG_PATH = os.path.join(BASE_PATH, 'bot.log')
    
    # Modifica i percorsi dei file CSV
    USERS_FILE = os.path.join(BASE_PATH, 'utenti.csv')
    INTERACTIONS_FILE = os.path.join(BASE_PATH, 'interazioni.csv')
    FEEDBACK_FILE = os.path.join(BASE_PATH, 'feedback.csv')
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    if not TELEGRAM_TOKEN:
        print("ERRORE: Token Telegram non trovato nel file .env")
        print("Directory corrente:", os.getcwd())
        print("Contenuto ENV:", os.environ)

    ADMIN_IDS = [int(id.strip()) for id in os.getenv('ADMIN_IDS', '').split(',')]
    MAX_LOGIN_ATTEMPTS = int(os.getenv('MAX_LOGIN_ATTEMPTS', '3'))
    LOGIN_TIMEOUT = int(os.getenv('LOGIN_TIMEOUT', '300'))

    @classmethod
    def is_admin(cls, user_id):
        """Verifica se un utente è admin"""
        return user_id in cls.ADMIN_IDS

def start_bot():
    if not configure_network():
        return False

    try:
        updater = get_updater()
        dp = updater.dispatcher

        dp.add_handler(CommandHandler("start", inizio))
        dp.add_handler(CommandHandler("rispond", rispondi_comando))
        dp.add_handler(CommandHandler("export", export_data_for_admin))
        dp.add_handler(CommandHandler("dashboard", admin_dashboard))
        dp.add_handler(CommandHandler("backup", force_backup))
        dp.add_handler(CommandHandler("cleanup", cleanup_old_files))
        dp.add_handler(CommandHandler("ordini_pendenti", admin_check_orders))
        dp.add_handler(CommandHandler("conferma_pagamento", admin_confirm_payment))
        dp.add_handler(CallbackQueryHandler(gestisci_click_pulsante))
        dp.add_handler(MessageHandler(Filters.text & ~Filters.command, gestisci_messaggio))
        dp.add_error_handler(error_handler)

        logger.info("Avvio polling...")
        updater.start_polling(drop_pending_updates=True)
        logger.info("Bot avviato con successo")
        updater.idle()
        return True
    except KeyboardInterrupt:
        logger.info("Interruzione manuale...")
        return True
    except Exception as e:
        logger.error(f"Errore avvio bot: {e}")
        return False

def backup_file(filename):
    """Crea un backup giornaliero dei file"""
    backup_dir = '/home/InstantSong/backup'
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)

    data = datetime.now().strftime('%Y-%m-%d')
    backup_file = f"{backup_dir}/{os.path.splitext(filename)[0]}_{data}.csv"

    # Crea backup solo se non esiste già per oggi
    if not os.path.exists(backup_file):
        shutil.copy2(filename, backup_file)
        print(f"Backup creato: {backup_file}")

def get_statistics():
    """Raccoglie statistiche dai file CSV"""
    stats = {
        'total_users': 0,
        'today_interactions': 0,
        'avg_rating': 0,
        'pending_orders': 0
    }

    # Conta utenti totali
    if os.path.exists('utenti.csv'):
        with open('utenti.csv', 'r', encoding='utf-8') as f:
            stats['total_users'] = sum(1 for line in f) - 1  # -1 per l'header

    # Conta interazioni di oggi
    today = datetime.now().strftime('%Y-%m-%d')
    if os.path.exists('interazioni.csv'):
        with open('interazioni.csv', 'r', encoding='utf-8') as f:
            stats['today_interactions'] = sum(
                1 for line in f if today in line
            )

    # Calcola media feedback
    if os.path.exists('feedback.csv'):
        total_stars = 0
        num_ratings = 0
        with open('feedback.csv', 'r', encoding='utf-8') as f:
            csv_reader = csv.DictReader(f)
            for row in csv_reader:
                try:
                    if row['stelle'] and row['stelle'].strip():
                        stars = int(row['stelle'])
                        total_stars += stars
                        num_ratings += 1
                except (ValueError, KeyError):
                    continue

        if num_ratings > 0:
            stats['avg_rating'] = round(total_stars / num_ratings, 1)

    # Conta ordini pendenti
    if os.path.exists('ordini_pending.csv'):
        with open('ordini_pending.csv', 'r', encoding='utf-8') as f:
            stats['pending_orders'] = sum(1 for line in f) - 1  # -1 per l'header

    return stats

def rotate_files():
    """Mantiene i file ad una dimensione gestibile"""
    MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

    for filename in ['interazioni.csv', 'feedback.csv']:
        if os.path.exists(filename) and os.path.getsize(filename) > MAX_FILE_SIZE:
            # Crea un file di archivio
            archive_name = f'archived_{filename}_{datetime.now().strftime("%Y%m")}.csv'
            shutil.move(filename, f'/home/InstantSong/backup/{archive_name}')

            # Crea un nuovo file con solo l'header
            with open(filename, 'w', encoding='utf-8') as f:
                if filename == 'interazioni.csv':
                    f.write('Data,Tipo,Nome,Username,ID,Contenuto\n')
                elif filename == 'feedback.csv':
                    f.write('username,user_id,stelle,commento,data\n')

def controlla_istanza():
    """Controlla se un'altra istanza del bot è già in esecuzione"""
    lockfile = '/tmp/instant-song-bot.lock'

    # Prima proviamo a rimuovere eventuali file di lock residui
    try:
        os.remove(lockfile)
    except FileNotFoundError:
        pass
    except PermissionError:
        print("Non posso rimuovere il file di lock esistente. Potrebbe esserci un'altra istanza attiva.")
        sys.exit(1)

    try:
        # Creiamo un nuovo file di lock
        lock = open(lockfile, 'w')
        fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock
    except IOError:
        print("Il bot è già in esecuzione!")
        sys.exit(1)
    except Exception as e:
        print(f"Errore nel controllo dell'istanza: {str(e)}")
        sys.exit(1)

def salva_utente(nome_utente, username, user_id):
    try:
        data_ora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        file_path = 'utenti.csv'

        # Verifica se l'utente esiste già
        utente_esistente = False
        if os.path.exists(file_path):
            with open(file_path, 'r', newline='', encoding='utf-8') as file:
                reader = csv.reader(file)
                headers = next(reader)  # Legge gli headers
                id_index = headers.index('ID')  # Usa gli headers per trovare l'indice dell'ID

                for row in reader:
                    if row[id_index] == str(user_id):
                        utente_esistente = True
                        break

        if not utente_esistente:
            if not os.path.exists(file_path):
                with open(file_path, 'w', newline='', encoding='utf-8') as file:
                    writer = csv.writer(file)
                    writer.writerow(['Data', 'Nome', 'Username', 'ID'])

            with open(file_path, 'a', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow([data_ora, nome_utente, username, user_id])

            # Crea backup
            backup_file(file_path)

    except Exception as e:
        print(f"Errore nel salvare l'utente: {str(e)}")
        traceback.print_exc()

def rispondi_comando(update, context):
    try:
        # Verifica se il messaggio è dall'admin
        user_id = str(update.message.from_user.id)
        if Config.is_admin(int(user_id)):
            # Verifica che ci siano abbastanza argomenti
            if len(context.args) < 2:
                update.message.reply_text("Formato corretto: /rispond user_id messaggio")
                return

            target_user_id = context.args[0]
            messaggio = ' '.join(context.args[1:])  # Unisce tutte le parole dopo l'user_id

            if messaggio.strip() == "":  # Verifica che il messaggio non sia vuoto
                update.message.reply_text("Il messaggio non può essere vuoto")
                return

            if invia_risposta(context, target_user_id, messaggio):
                update.message.reply_text("Messaggio inviato con successo!")
            else:
                update.message.reply_text("Errore nell'invio del messaggio")
        else:
            update.message.reply_text("Non hai i permessi per usare questo comando")
    except Exception as e:
        print(f"Errore in rispondi_comando: {str(e)}")
        update.message.reply_text("Formato corretto: /rispond user_id messaggio")

def invia_risposta(context, user_id, messaggio):
    try:
        context.bot.send_message(
            chat_id=user_id,
            text=messaggio,
            parse_mode='Markdown'
        )
        return True
    except Exception as e:
        print(f"Errore nell'invio del messaggio: {e}")
        return False

def salva_interazione(tipo, nome_utente, username, user_id, contenuto):
    try:
        data_ora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        file_path = 'interazioni.csv'
        print(f"Tentativo di salvare l'interazione nel file: {file_path}")

        # Creazione file se non esiste
        if not os.path.exists(file_path):
            print("File non esistente, creo intestazioni...")
            with open(file_path, 'w', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow(['Data', 'Tipo', 'Nome', 'Username', 'ID', 'Contenuto'])
            os.chmod(file_path, 0o666)

        # Aggiunta nuova riga
        with open(file_path, 'a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow([data_ora, tipo, nome_utente, username, user_id, contenuto])
        print("Salvato localmente con successo")

        # Crea backup
        backup_file(file_path)

    except Exception as e:
        print(f"Errore nel salvataggio: {str(e)}")
        print(f"Tipo di errore: {type(e)}")
        traceback.print_exc()

def gestisci_messaggio(update, context):
    try:
        messaggio_utente = update.message.text
        nome_utente = update.message.from_user.first_name
        username = update.message.from_user.username
        user_id = update.message.from_user.id

        # Log del messaggio ricevuto
        logger.info(f"Messaggio ricevuto da: {nome_utente} (ID: {user_id})")

        # Inoltro del messaggio all'admin
        for admin_id in Config.ADMIN_IDS:
            testo_admin = (
                f"📩 Nuovo messaggio da:\n"
                f"👤 Nome: {nome_utente}\n"
                f"🆔 ID: {user_id}\n"
                f"👤 Username: @{username}\n"
                f"💭 Messaggio:\n{messaggio_utente}\n\n"
                f"Per rispondere usa:\n"
                f"`/rispond {user_id} <tua risposta>`"
            )
            try:
                context.bot.send_message(
                    chat_id=admin_id,
                    text=testo_admin,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Errore nell'invio della notifica all'admin {admin_id}: {str(e)}")

        # Salva l'utente
        salva_utente(nome_utente, username, user_id)

        # Se siamo in attesa di un commento
        if context.user_data.get('attesa_commento'):
            # Salva l'interazione come commento
            salva_interazione('commento', nome_utente, username, user_id, messaggio_utente)
            context.user_data['attesa_commento'] = False
            messaggio = update.message.reply_text("Grazie per il tuo feedback! Lo leggeremo con attenzione ❤️")
        else:
            # Salva l'interazione come messaggio normale
            salva_interazione('messaggio', nome_utente, username, user_id, messaggio_utente)
            messaggio = update.message.reply_text("Grazie del messaggio! Ti risponderemo appena possibile.")

        # Elimina il messaggio dopo 5 secondi
        context.job_queue.run_once(lambda _: messaggio.delete(), 5)

    except Exception as e:
        logger.error(f"Errore in gestisci_messaggio: {e}")
        print(f"Errore in gestisci_messaggio: {e}")
        update.message.reply_text("Mi dispiace, c'è stato un errore. Riprova più tardi.")

def crea_tastiera_menu_principale():
    return [
        [
            InlineKeyboardButton("🎵 Ascolta Demo", callback_data='demo'),
            InlineKeyboardButton("💝 Ordina Ora", callback_data='ordina')
        ],
        [
            InlineKeyboardButton("💫 Come Funziona", callback_data='processo'),
            InlineKeyboardButton("💰 Listino", callback_data='prezzi')
        ],
        [
            InlineKeyboardButton("✨ Chi Siamo", callback_data='info'),
            InlineKeyboardButton("📞 Contattaci", callback_data='supporto')
        ],
        [
            InlineKeyboardButton("❓ FAQ", callback_data='faq'),
            InlineKeyboardButton("⭐ Feedback", callback_data='feedback')
        ]
    ]

def inizio(update, context):
    tastiera = crea_tastiera_menu_principale()
    reply_markup = InlineKeyboardMarkup(tastiera)
    testo = ("✨ *Benvenuto in Instant Song* ✨\n\n"
            "🎵 Trasformiamo i tuoi sentimenti in musica! 🎵\n\n"
            "Creiamo canzoni uniche e personalizzate per:\n"
              "• 💝 Dediche d'amore speciali\n"
              "• 🎂 Compleanni indimenticabili\n"
              "• 👰 Matrimoni da sogno\n"
              "• 🎓 Lauree memorabili\n"
              "• 🏢 Jingle aziendali accattivanti\n"
              "• 🏫 Oratori e Centri Giovanili\n"
              "• 👥 Gruppi e Associazioni\n"
              "• 💫 Eventi Comunitari\n\n"
            "*Ogni canzone sarà creata su misura ed è Copyright Free: è tua e ne puoi fare quello che vuoi!*\n\n"
            "Seleziona un'opzione per iniziare il tuo viaggio musicale 👇")

    if update.message:
        update.message.reply_text(testo, parse_mode='Markdown', reply_markup=reply_markup)
    else:
        update.callback_query.edit_message_text(testo, parse_mode='Markdown', reply_markup=reply_markup)

def invia_demo_audio(update, context, demo_id, caption=None):
    """
    Invia un file audio demo direttamente tramite Telegram
    """
    demo_files = {
        'demo1': '/home/InstantSong/demo/demo_dichiarazione.mp3',
        'demo2': '/home/InstantSong/demo/demo_compleanno.mp3',
        'demo3': '/home/InstantSong/demo/demo_coop.mp3',
        'demo4': '/home/InstantSong/demo/demo_centro giovanile.mp3',
        'demo5': '/home/InstantSong/demo/Slow morning_1.mp3'  # Corretto il nome del file
    }

    try:
        if demo_id in demo_files:
            with open(demo_files[demo_id], 'rb') as audio:
                context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=audio,
                    caption=caption or "🎵 Ascolta la nostra demo!",
                    parse_mode='Markdown'
                )
        else:
            update.callback_query.answer("Demo non disponibile")
    except Exception as e:
        print(f"Errore nell'invio dell'audio: {str(e)}")
        update.callback_query.answer("Errore nell'invio dell'audio")

def gestisci_click_pulsante(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    if query.data == 'start' or query.data == 'menu_principale':
        return inizio(update, context)
    elif query.data == 'info':
        tastiera = [
            [InlineKeyboardButton("🏠 Menu Principale", callback_data='start')]
        ]
        reply_markup = InlineKeyboardMarkup(tastiera)
        query.edit_message_text(
            text="✨ *Chi Siamo*\n\n"
                 "Siamo un inedito connubio di creatività umana e intelligenza artificiale... con quel pizzico di ironia che amalgama il tutto... \n"
                 "Abbiamo una missione: trasformare i tuoi sentimenti in melodie indimenticabili! 🎵\n\n"
                 "*Perché sceglierci:*\n\n"
                 "🎯 *Personalizzazione Totale*\n"
                 "• Ogni canzone è unica come la tua storia\n"
                 "• Testo e musica creati su misura\n"
                 "• Attenzione a ogni dettaglio\n\n"
                 "⚡️ *Processo Trasparente*\n"
                 "• Prova gratuita per ogni cliente\n"
                 "• Demo prima del pagamento\n"
                 "• Sistema crediti chiaro e flessibile\n\n"
                 "💎 *Alta Qualità*\n"
                 "• Registrazione professionale\n"
                 "• File mp3 in alta definizione\n"
                 "• Copyright Free - la canzone è tua\n\n"
                 "🎓 *Esperienza*\n"
                 "• Team di professionisti\n"
                 "• Centinaia di clienti soddisfatti\n\n"
                 "• Supporto dedicato 7 giorni su 7\n\n"
                 "Ascolta le nostre demo e lasciati ispirare! 🎵",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    elif query.data in ['prova_gratuita', 'my_credits'] or query.data.startswith('buy_'):
        handle_credit_buttons(update, context)

    elif query.data == 'demo':
        tastiera = [
            [InlineKeyboardButton("💝 Demo 1: Dichiarazione d'Amore", callback_data='play_demo1')],
            [InlineKeyboardButton("🎂 Demo 2: Canzone di Compleanno", callback_data='play_demo2')],
            [InlineKeyboardButton("🏢 Demo 3: Coop. Soc. Fruts di Bosc (UD)", callback_data='play_demo3')],
            [InlineKeyboardButton("🌟 Demo 4: ONLUS La Fabbrica dei Sogni (BG)", callback_data='play_demo4')],
            [InlineKeyboardButton("📻 Demo 5: Slow Morning - In Radio!", callback_data='play_demo5')],
            [InlineKeyboardButton("🏠 Menu Principale", callback_data='start')]
        ]
        reply_markup = InlineKeyboardMarkup(tastiera)
        query.edit_message_text(
            text="🎵 *Le Nostre Demo*\n\n"
                 "📻 *Trasmessi in Radio!*\n"
                 "[Andrea Nordio](https://www.piterpan.it/artists/andrea-nordio/), voce storica di Radio Piterpan, ha apprezzato così tanto le nostre produzioni da trasmetterle in radio! Ascolta 'Slow Morning', uno dei nostri brani andati in onda su una delle radio più ascoltate del Nord Est.\n\n"
                 "*Ascolta le nostre demo:*\n"
                 "• Demo 1 e 2: Canzoni ad personam\n"
                 "• Demo 3 e 4: Progetti per realtà sociali\n"
                 "• Demo 5: Uno dei nostri brani trasmessi in radio\n\n"
                 "Clicca su una demo per ascoltarla direttamente qui!\n\n"
                 "Pronto per avere la tua canzone personalizzata? 😊",
            parse_mode='Markdown',
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )

    elif query.data.startswith('play_demo'):
        demo_id = query.data.replace('play_demo', '')

        # Definizione dei testi delle didascalie per ciascuna demo
        demo_captions = {
            '1': "🎵 *Demo 1: Dichiarazione d'Amore*\nAscolta la nostra demo!",
            '2': "🎵 *Demo 2: Canzone di Compleanno*\nAscolta la nostra demo!",
            '3': "🎵 *Demo 3: Cooperativa Sociale*\n[Fruts di Bosc](https://www.facebook.com/FrutsDiBosc/?locale=it_IT) - Udine\nAscolta la nostra demo!",
            '4': "🎵 *Demo 4: Centro Giovanile*\n[La Fabbrica dei Sogni](https://www.fabbricasogni.it/) - Bergamo\nAscolta la nostra demo!",
            '5': "📻 *Demo 5: Slow Morning*\nUno dei nostri brani scelti da Andrea Nordio e trasmessi su Radio Piterpan!\nAscolta la nostra demo!"
        }

        invia_demo_audio(update, context, f'demo{demo_id}', demo_captions[demo_id])

        tastiera = [
            [InlineKeyboardButton("💝 Demo 1: Dichiarazione d'Amore", callback_data='play_demo1')],
            [InlineKeyboardButton("🎂 Demo 2: Canzone di Compleanno", callback_data='play_demo2')],
            [InlineKeyboardButton("🏢 Demo 3: Coop. Soc. Fruts di Bosc (UD)", callback_data='play_demo3')],
            [InlineKeyboardButton("🌟 Demo 4: ONLUS La Fabbrica dei Sogni (BG)", callback_data='play_demo4')],
            [InlineKeyboardButton("📻 Demo 5: Slow Morning - In Radio!", callback_data='play_demo5')],
            [InlineKeyboardButton("🏠 Menu Principale", callback_data='start')]
        ]
        reply_markup = InlineKeyboardMarkup(tastiera)
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🎵 *Le Nostre Demo*\n\n"
                 "Ecco le nostre demo disponibili. Ascoltale tutte!\n\n"
                 "Quale ti è piaciuta di più? 😊",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    elif query.data == 'prezzi':
        tastiera = [
            [InlineKeyboardButton("🎁 Prova Gratuita", callback_data='prova_gratuita')],
            [
                InlineKeyboardButton("🎵 Starter: 10€", callback_data='buy_starter'),
                InlineKeyboardButton("🌟 Popular: 25€", callback_data='buy_popular')
            ],
            [InlineKeyboardButton("💎 Premium: 65€", callback_data='buy_premium')],
            [InlineKeyboardButton("💝 Offerta Libera", callback_data='offerta_libera')],
            [
                InlineKeyboardButton("💳 I miei crediti", callback_data='my_credits'),
                InlineKeyboardButton("❓ Come funziona", callback_data='help_credits')
            ],
            [InlineKeyboardButton("🏠 Menu Principale", callback_data='start')]
        ]
        reply_markup = InlineKeyboardMarkup(tastiera)
        query.edit_message_text(
            text="💰 *LISTINO PREZZI - Sistema a Crediti*\n\n"
                 "🎁 *PROVA GRATUITA*\n"
                 "• Un ritornello personalizzato\n"
                 "• Anteprima del testo\n\n"
                 "📦 *PACCHETTI CREDITI:*\n\n"
                 "🎵 *Starter: 10€ = 4 crediti*\n"
                 "• 1 canzone completa + 2 modifiche extra\n"
                 "• oppure 2 canzoni base\n\n"
                 "🌟 *Popular: 25€ = 12 crediti*\n"
                 "• 4 canzoni complete + 4 modifiche extra\n"
                 "• oppure 6 canzoni base\n\n"
                 "💎 *Premium: 65€ = 32 crediti + 1 canzone gratuita*\n"
                 "• 12 canzoni complete + 8 modifiche extra\n"
                 "• oppure 16 canzoni base\n"
                 "• + 1 canzone BONUS\n\n"
                 "*Come si usano i crediti:*\n"
                 "• 2 crediti = 1 canzone completa\n"
                 "• 1 credito = 1 modifica aggiuntiva\n"
                 "• 1 modifica gratuita inclusa in ogni canzone\n\n"
                 "✨ *I crediti non scadono mai!*\n\n"
                 "💝 *Vuoi sostenerci?*\n"
                 "Se apprezzi il nostro lavoro, puoi fare un'offerta libera per aiutarci a crescere e migliorare sempre di più! Ogni contributo extra sarà reinvestito nella qualità delle nostre produzioni.",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    elif query.data == 'offerta_libera':
        tastiera = [
            [InlineKeyboardButton("🔙 Torna al Listino", callback_data='prezzi')]
        ]
        reply_markup = InlineKeyboardMarkup(tastiera)
        query.edit_message_text(
            text="💝 *Offerta Libera - Sostieni Instant Song*\n\n"
                 "Grazie per voler supportare il nostro progetto! La tua generosità ci aiuta a:\n\n"
                 "🎵 Migliorare la qualità delle produzioni\n"
                 "🎨 Sviluppare nuovi stili e arrangiamenti\n"
                 "🎼 Investire in strumenti e tecnologie\n"
                 "💫 Creare nuove opportunità creative\n\n"
                 "*Modalità di pagamento disponibili:*\n\n"
                 "💳 *PostePay*\n"
                 "IBAN: IT37T3608105138234713496\n"
                 "Causale: Offerta InstantSong\n\n"
                 "💸 *PayPal*\n"
                 "[Clicca qui per donare](https://paypal.me/InstantSong)\n\n"
                 "🙏 Dopo aver effettuato il pagamento, scrivici un messaggio qui nel bot per permetterci di ringraziarti personalmente!\n\n"
                 "❤️ Ogni contributo, grande o piccolo, fa la differenza!\n"
                 "_Il tuo supporto ci motiva a dare sempre il massimo._",
            parse_mode='Markdown',
            disable_web_page_preview=True,
            reply_markup=reply_markup
        )

    elif query.data == 'help_credits':
        tastiera = [[InlineKeyboardButton("🔙 Torna al Listino", callback_data='prezzi')]]
        reply_markup = InlineKeyboardMarkup(tastiera)
        query.edit_message_text(
            text="❓ *Come Funzionano i Crediti*\n\n"
                 "*1. Acquisto Crediti*\n"
                 "• Scegli uno dei pacchetti disponibili\n"
                 "• Effettua il pagamento usando il codice fornito\n"
                 "• I crediti vengono aggiunti al tuo account\n\n"
                 "*2. Utilizzo Crediti*\n"
                 "• 2 crediti = 1 canzone completa\n"
                 "• 1 credito = 1 modifica aggiuntiva\n"
                 "• 1 modifica gratuita inclusa\n\n"
                 "*3. Vantaggi*\n"
                 "• Prova il servizio gratuitamente\n"
                 "• I crediti non scadono mai\n"
                 "• Più crediti acquisti, più risparmi\n\n"
                 "*4. Esempio Pratico*\n"
                 "Con il pacchetto Starter (4 crediti) puoi avere:\n"
                 "• 1 canzone completa + 2 modifiche extra\n"
                 "• oppure 2 canzoni base\n\n"
                 "*5. Trasparenza*\n"
                 "• Prova il servizio gratis\n"
                 "• Vedi sempre i crediti residui\n"
                 "• Assistenza sempre disponibile",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    elif query.data == 'processo':
        tastiera = [
            [InlineKeyboardButton("🎁 Prova Gratuita", callback_data='prova_gratuita')],
            [InlineKeyboardButton("🎵 Ascolta le Demo", callback_data='demo')],
            [InlineKeyboardButton("🏠 Menu Principale", callback_data='start')]
        ]
        reply_markup = InlineKeyboardMarkup(tastiera)
        query.edit_message_text(
            text="💫 *Come Funziona InstantSong*\n\n"
                 "1️⃣ *Prova il Servizio*\n"
                 "• Raccontaci la tua idea\n"
                 "• Ricevi gratuitamente un breve ritornello\n"
                 "• Valuta la qualità del nostro lavoro\n\n"
                 "2️⃣ *Acquista i Crediti*\n"
                 "• Starter: 4 crediti (10€)\n"
                 "• Popular: 12 crediti (25€)\n"
                 "• Premium: 32 crediti + bonus (65€)\n\n"
                 "3️⃣ *Richiedi la Tua Canzone*\n"
                 "• Una canzone = 2 crediti\n"
                 "• 1 modifica gratuita inclusa\n"
                 "• Modifiche extra = 1 credito ciascuna\n\n"
                 "4️⃣ *Cosa Ricevi*\n"
                 "• File mp3 professionale (320kbps)\n"
                 "• Testo completo della canzone\n"
                 "• Copyright Free - La canzone è completamente tua\n\n"
                 "5️⃣ *Supporto e Assistenza*\n"
                 "• Risposta entro 24h nei giorni feriali\n"
                 "• Assistenza via chat per ogni dubbio\n"
                 "• I crediti non scadono mai\n\n"
                 "🎁 *Inizia con la Prova Gratuita*\n"
                 "Clicca il pulsante qui sotto per ricevere il tuo ritornello personalizzato.",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    elif query.data == 'ordina':
        tastiera = [
            [InlineKeyboardButton("🏠 Menu Principale", callback_data='start')]
        ]
        reply_markup = InlineKeyboardMarkup(tastiera)
        query.edit_message_text(
            text="💝 *Richiedi la Tua Canzone*\n\n"
                 "Raccontaci cosa desideri:\n\n"
                 "1️⃣ *Tipo di canzone*\n"
                 "• Per chi è?\n"
                 "• Per quale occasione?\n"
                 "• Che emozioni vuoi trasmettere?\n\n"
                 "2️⃣ *Dettagli importanti*\n"
                 "• Nomi da includere\n"
                 "• Ricordi speciali\n"
                 "• Messaggi chiave\n"
                 "• Genere musicale preferito (pop, rock, ballad...)\n\n"
                 "*Scrivi qui il tuo messaggio* e il nostro team creativo ti risponderà appena possibile! ⚡️",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    elif query.data == 'supporto':
        tastiera = [
            [InlineKeyboardButton("🏠 Menu Principale", callback_data='start')]
        ]
        reply_markup = InlineKeyboardMarkup(tastiera)
        query.edit_message_text(
            text="📞 *Contattaci*\n\n"
                 "Hai domande? Siamo qui per aiutarti!\n\n"
                 "• ✍️ Scrivi direttamente qui nel bot\n"
                 "• 📧 Invia una mail a instantsong.info@gmail.com\n\n"
                 "Ti risponderemo appena possibile!",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    elif query.data == 'faq':
        tastiera = [
            [InlineKeyboardButton("🏠 Menu Principale", callback_data='start')]
        ]
        reply_markup = InlineKeyboardMarkup(tastiera)
        query.edit_message_text(
            text="❓ *Domande Frequenti*\n\n"
                 "*🎵 Come funziona il servizio?*\n"
                 "• Ci racconti la tua idea\n"
                 "• Ricevi una prova gratuita\n"
                 "• Se ti piace, procedi con l'acquisto\n\n"
                 "*🎯 Perché dovrei scegliere InstantSong?*\n"
                 "• Ogni canzone è unica e personalizzata\n"
                 "• Provi gratuitamente prima di decidere\n"
                 "• Centinaia di clienti soddisfatti\n\n"
                 "*🤔 La canzone sarà davvero personalizzata?*\n"
                 "• Sì! Ogni brano è creato da zero\n"
                 "• Includiamo nomi, ricordi e dettagli specifici\n"
                 "• Non usiamo mai basi preconfezionate\n\n"
                 "*🎼 Che tipo di canzoni fate?*\n"
                 "• Dediche d'amore\n"
                 "• Canzoni di compleanno\n"
                 "• Regali di matrimonio\n"
                 "• Jingle aziendali\n"
                 "• Canzoni per eventi e ricorrenze\n"
                 "• Progetti per realtà sociali\n\n"
                 "*💫 Come garantite la qualità?*\n"
                 "• Demo gratuita per valutare il nostro lavoro\n"
                 "• Modifica gratuita inclusa\n"
                 "• Registrazione professionale in studio\n"
                 "• Ascolti il risultato prima di pagare\n\n"
                 "*⏱ Quanto tempo ci vuole?*\n"
                 "• Demo gratuita: 24-48h\n"
                 "• Canzone completa: 2-3 giorni\n"
                 "• Modifiche: 24h\n\n"
                 "*💳 Come funzionano i crediti?*\n"
                 "• 2 crediti = 1 canzone completa\n"
                 "• 1 credito = 1 modifica aggiuntiva\n"
                 "• 1 modifica gratuita sempre inclusa\n"
                 "• I crediti non scadono mai\n\n"
                 "*🎸 Posso scegliere lo stile musicale?*\n"
                 "• Sì! Realizziamo qualsiasi genere\n"
                 "• Pop, Rock, Ballad, Rap...\n"
                 "• Puoi indicare canzoni di riferimento\n\n"
                 "*✨ Cosa rende unico il vostro servizio?*\n"
                 "• Prova gratuita senza impegno\n"
                 "• Sistema a crediti flessibile\n"
                 "• Possibilità di chiedere modifiche\n"
                 "• La canzone diventa completamente tua\n\n"
                 "*📝 Come funzionano le modifiche?*\n"
                 "• Prima modifica sempre gratuita\n"
                 "• Puoi modificare testo e musica\n"
                 "• Modifiche extra: 1 credito ciascuna\n"
                 "• Supporto dedicato per ogni richiesta\n\n"
                 "*🎶 Cosa ricevo esattamente?*\n"
                 "• File mp3 professionale (320kbps)\n"
                 "• Testo completo della canzone\n"
                 "• Diritti completi sulla canzone\n"
                 "• Possibilità di uso commerciale\n\n"
                 "*💰 Come posso pagare?*\n"
                 "• Bonifico bancario\n"
                 "• PostePay\n"
                 "• PayPal\n"
                 "• Pagamenti sicuri e verificati\n\n"
                 "*🔒 È sicuro acquistare?*\n"
                 "• Provi gratis prima di acquistare\n"
                 "• Paghi solo se soddisfatto\n"
                 "• Migliaia di clienti soddisfatti\n"
                 "• Servizio attivo dal 2023\n\n"
                 "*❓ Altre domande?*\n"
                 "Scrivici! Siamo qui per aiutarti 😊\n"
                 "Risposta garantita entro 24h",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    elif query.data == 'feedback':
        tastiera = [
            [
                InlineKeyboardButton("⭐", callback_data='stella_1'),
                InlineKeyboardButton("⭐⭐", callback_data='stella_2'),
                InlineKeyboardButton("⭐⭐⭐", callback_data='stella_3'),
                InlineKeyboardButton("⭐⭐⭐⭐", callback_data='stella_4'),
                InlineKeyboardButton("⭐⭐⭐⭐⭐", callback_data='stella_5')
            ],
            [InlineKeyboardButton("💌 Lascia un commento", callback_data='commento')],
            [InlineKeyboardButton("🏠 Menu Principale", callback_data='start')]
        ]
        reply_markup = InlineKeyboardMarkup(tastiera)
        query.edit_message_text(
            text="✨ *Feedback e Ringraziamenti*\n\n"
                 "La tua opinione è importante per noi!\n\n"
                 "🌟 *Valuta il nostro servizio*\n"
                 "Seleziona le stelle (da 1 a 5)\n\n"
                 "💭 *Lascia un commento*\n"
                 "Racconta la tua esperienza e le reazioni dei destinatari\n\n"
                 "❤️ *Se ti è piaciuto il nostro servizio*\n"
                 "Aiutaci a crescere! Passa parola ad amici e familiari\n\n"
                 "_I commenti ci aiuteranno a migliorare il servizio_",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    elif query.data.startswith('stella_'):
        numero_stelle = int(query.data.replace('stella_', ''))
        salva_feedback(query.from_user, stelle=numero_stelle)

        tastiera = [
            [InlineKeyboardButton("💌 Lascia un commento", callback_data='commento')],
            [InlineKeyboardButton("🏠 Menu Principale", callback_data='start')]
        ]
        reply_markup = InlineKeyboardMarkup(tastiera)

        query.edit_message_text(
            text=f"✨ *Grazie per la tua valutazione di {numero_stelle} stelle!*\n\n"
                 "Vuoi aggiungere un commento alla tua valutazione?",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    elif query.data == 'commento':
        context.user_data['attesa_commento'] = True

        tastiera = [[InlineKeyboardButton("🏠 Menu Principale", callback_data='start')]]
        reply_markup = InlineKeyboardMarkup(tastiera)

        query.edit_message_text(
            text="💭 *Lascia il tuo commento*\n\n"
                 "Scrivi qui sotto il tuo commento o la tua esperienza.\n"
                 "Ci aiuterà a migliorare il nostro servizio!",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

def gestisci_feedback(update, context):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Definisci i pulsanti delle stelle
    pulsanti_stelle = [
        [InlineKeyboardButton("⭐️", callback_data='stella_1'),
         InlineKeyboardButton("⭐️⭐️", callback_data='stella_2'),
         InlineKeyboardButton("⭐️⭐️⭐️", callback_data='stella_3')],
        [InlineKeyboardButton("⭐️⭐️⭐️⭐️", callback_data='stella_4'),
         InlineKeyboardButton("⭐️⭐️⭐️⭐️⭐️", callback_data='stella_5')],
        [InlineKeyboardButton("✍️ Lascia un commento", callback_data='commento')]
    ]
    reply_markup = InlineKeyboardMarkup(pulsanti_stelle)

    update.callback_query.edit_message_text(
        "⭐ Per favore, valuta la tua esperienza con noi!",
        reply_markup=reply_markup
    )


def salva_feedback(user, stelle=None, commento=None):
    logger.info(f"Feedback ricevuto da: {user.username} (ID: {user.id}) - Stelle: {stelle}")
    import csv
    dati = {
        'username': user.username or 'Anonimo',
        'user_id': user.id,
        'stelle': stelle,
        'commento': commento,
        'data': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    with open('feedback.csv', mode='a', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=dati.keys())
        if file.tell() == 0:  # Scrivi l'intestazione se il file è vuoto
            writer.writeheader()
        writer.writerow(dati)


def get_request_kwargs():
    return {
        'read_timeout': 6,
        'connect_timeout': 7,
        'request_kwargs': {
            'proxy_url': None  # Questo disabilita l'uso del proxy
        }
    }

def export_data_for_admin(update, context):
    """Permette all'admin di ricevere i file come documento"""
    if not Config.is_admin(update.message.from_user.id):
        return

    files_to_send = [
        'utenti.csv',
        'interazioni.csv',
        'feedback.csv',
        'ordini_pending.csv',
        'crediti_utenti.csv'
    ]

    for filename in files_to_send:
        if os.path.exists(filename):
            # Verifica che il file non sia vuoto
            if os.path.getsize(filename) > 0:
                with open(filename, 'rb') as f:
                    try:
                        context.bot.send_document(
                            chat_id=update.message.chat_id,
                            document=f,
                            filename=filename,
                            caption=f"Export {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                        )
                    except Exception as e:
                        update.message.reply_text(f"Errore nell'invio del file {filename}: {str(e)}")

def admin_dashboard(update, context):
    """Crea una dashboard con statistiche principali"""
    if not Config.is_admin(update.message.from_user.id):
        return

    stats = get_statistics()

    message = (
        "📊 *Dashboard InstantSong*\n\n"
        f"👥 Utenti Totali: {stats['total_users']}\n"
        f"💬 Interazioni Oggi: {stats['today_interactions']}\n"
        f"⭐ Feedback Medi: {stats['avg_rating']:.1f}/5\n"
        f"📝 Ordini Pendenti: {stats['pending_orders']}\n\n"
        "Usa /export per scaricare i file CSV"
    )

    update.message.reply_text(message, parse_mode='Markdown')

def force_backup(update, context):
    """Forza un backup manuale dei file"""
    if not Config.is_admin(update.message.from_user.id):
        return

    files_to_backup = [
        'utenti.csv',
        'interazioni.csv',
        'feedback.csv',
        'ordini_pending.csv',
        'crediti_utenti.csv'
    ]

    for filename in files_to_backup:
        if os.path.exists(filename):
            backup_file(filename)

    update.message.reply_text("Backup completato! ✅")

def cleanup_on_exit():
    """Pulisce il file di lock all'uscita"""
    lockfile = '/tmp/instant-song-bot.lock'
    try:
        if os.path.exists(lockfile):
            os.remove(lockfile)
    except Exception as e:
        print(f"Errore nella pulizia: {e}")

def configure_network():
    try:
        import urllib3
        urllib3.disable_warnings()
        
        # Configurazione proxy PythonAnywhere
        os.environ['HTTP_PROXY'] = 'http://proxy.pythonanywhere.com:8000'
        os.environ['HTTPS_PROXY'] = 'http://proxy.pythonanywhere.com:8000'
        
        return True
    except Exception as e:
        logger.error(f"Errore nella configurazione di rete: {e}")
        return False

def cleanup_old_files(update, context):
    """Pulisce i file vecchi e fa la rotazione"""
    if not Config.is_admin(update.message.from_user.id):
        return

    try:
        # Richiama la funzione di rotazione dei file
        rotate_files()
        update.message.reply_text("Pulizia file completata! ✅")
    except Exception as e:
        logger.error(f"Errore durante la pulizia dei file: {e}")
        update.message.reply_text("Si è verificato un errore durante la pulizia dei file.")

def rotate_files():
    """Mantiene i file ad una dimensione gestibile"""
    MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

    for filename in ['interazioni.csv', 'feedback.csv']:
        if os.path.exists(filename) and os.path.getsize(filename) > MAX_FILE_SIZE:
            try:
                # Crea un file di archivio
                archive_name = f'archived_{filename}_{datetime.now().strftime("%Y%m")}.csv'
                backup_dir = '/home/InstantSong/backup'

                # Crea la directory di backup se non esiste
                if not os.path.exists(backup_dir):
                    os.makedirs(backup_dir)

                # Sposta il file nella directory di backup
                shutil.move(filename, f'{backup_dir}/{archive_name}')

                # Crea un nuovo file con solo l'header
                with open(filename, 'w', encoding='utf-8') as f:
                    if filename == 'interazioni.csv':
                        f.write('Data,Tipo,Nome,Username,ID,Contenuto\n')
                    elif filename == 'feedback.csv':
                        f.write('username,user_id,stelle,commento,data\n')

                logger.info(f"File {filename} archiviato come {archive_name}")

            except Exception as e:
                logger.error(f"Errore durante la rotazione del file {filename}: {e}")
                raise

def get_updater():
    from telegram.ext import Updater
    
    # Configurazione specifica per PythonAnywhere
    REQUEST_KWARGS = {
        'proxy_url': 'http://proxy.pythonanywhere.com:8000'
    }
    
    try:
        # Crea l'updater con la configurazione proxy di PythonAnywhere
        updater = Updater(
            token=Config.TELEGRAM_TOKEN,
            use_context=True,
            request_kwargs=REQUEST_KWARGS
        )
        return updater
    except Exception as e:
        logger.error(f"Errore nella creazione dell'updater: {e}")
        raise

def configure_network():
    try:
        import urllib3
        urllib3.disable_warnings()
        
        # Configurazione specifica per PythonAnywhere
        os.environ['HTTPS_PROXY'] = 'http://proxy.server:3128'
        os.environ['HTTP_PROXY'] = 'http://proxy.server:3128'
        
        # Configurazione certificati
        os.environ['REQUESTS_CA_BUNDLE'] = '/etc/ssl/certs/ca-certificates.crt'
        os.environ['SSL_CERT_FILE'] = '/etc/ssl/certs/ca-certificates.crt'
        
        return True
        
    except Exception as e:
        logger.error(f"Errore nella configurazione di rete: {e}")
        return False

def handle_network_error(update, context):
    try:
        # Log dell'errore
        logger.error(f"Errore di rete: {context.error}")
        
        # Gestione specifica per PythonAnywhere
        if "Operation not permitted" in str(context.error):
            logger.info("Errore di permessi PythonAnywhere, riprovo...")
            time.sleep(5)
            return
            
        if "Connection reset by peer" in str(context.error):
            logger.info("Connessione resettata, riprovo...")
            time.sleep(5)
            return
    except Exception as e:
        logger.error(f"Errore nel gestore errori: {e}")

def configure_network():
    # Rimuovi proxy
    proxy_vars = [
        'http_proxy', 'https_proxy', 'ftp_proxy', 'all_proxy',
        'HTTP_PROXY', 'HTTPS_PROXY', 'FTP_PROXY', 'ALL_PROXY',
        'SOCKS_PROXY', 'socks_proxy'
    ]

    for var in proxy_vars:
        if var in os.environ:
            del os.environ[var]

    os.environ['NO_PROXY'] = '*'
    os.environ['REQUESTS_CA_BUNDLE'] = '/etc/ssl/certs/ca-certificates.crt'
    os.environ['SSL_CERT_FILE'] = '/etc/ssl/certs/ca-certificates.crt'

    return True  # Rimuovi test_connection() e ritorna sempre True

def test_connection():
    try:
        from urllib3.util.retry import Retry
        from urllib3.poolmanager import PoolManager

        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504]
        )

        http = PoolManager(
            timeout=10.0,
            retries=retry_strategy,
            maxsize=10
        )

        response = http.request('GET', 'https://api.telegram.org/bot{token}/getMe'.format(
            token=Config.TELEGRAM_TOKEN
        ))

        if response.status == 200:
            logger.info("Connessione a Telegram verificata")
            return True

    except Exception as e:
        logger.error(f"Errore di connessione: {e}")
        return False

    return False

def main():
    try:
        # Creo l'updater
        updater = get_updater()
        dp = updater.dispatcher
        
        # Aggiungo il gestore degli errori
        dp.add_error_handler(handle_network_error)
        
        # Registro gli handler
        dp.add_handler(CommandHandler("start", inizio))
        dp.add_handler(CommandHandler("rispond", rispondi_comando))
        dp.add_handler(CommandHandler("export", export_data_for_admin))
        dp.add_handler(CommandHandler("dashboard", admin_dashboard))
        dp.add_handler(CommandHandler("backup", force_backup))
        dp.add_handler(CommandHandler("cleanup", cleanup_old_files))
        dp.add_handler(CommandHandler("ordini_pendenti", admin_check_orders))
        dp.add_handler(CommandHandler("conferma_pagamento", admin_confirm_payment))
        dp.add_handler(CallbackQueryHandler(gestisci_click_pulsante))
        dp.add_handler(MessageHandler(Filters.text & ~Filters.command, gestisci_messaggio))
        
        # Avvio il polling
        logger.info("Avvio polling...")
        updater.start_polling(drop_pending_updates=True)
        logger.info("Bot avviato con successo")
        updater.idle()
        
    except Exception as e:
        logger.error(f"Errore nell'avvio del bot: {e}")
        
    finally:
        if 'lock' in locals():
            cleanup_on_exit()

if __name__ == "__main__":
    main()
