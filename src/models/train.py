import pandas as pd
from sklearn import model_selection as ms
from sklearn.feature_extraction.text import TfidfVectorizer as tf
from sklearn.linear_model import LogisticRegression as lr
from sklearn.metrics import classification_report
import joblib

def train_model(caminho_dados):
    df = pd.read_csv(caminho_dados)
    df = df[df['sentiment'] != 'Neutro']

    X_train, X_test, y_train, y_test = ms.train_test_split(df['content'], df['sentiment'], test_size=0.2, random_state=42, stratify=df['sentiment'])

    vetorizador = tf()
    X_train_tfidf = vetorizador.fit_transform(X_train)
    X_test_tfidf = vetorizador.transform(X_test)

    modelo = lr()
    modelo.fit(X=X_train_tfidf, y=y_train)

    y_pred = modelo.predict(X_test_tfidf)

    relatorio = classification_report(y_true=y_test, y_pred=y_pred)

    print(relatorio)

    joblib.dump(modelo, 'artifacts/sentiment_model.pkl')
    joblib.dump(vetorizador, 'artifacts/tfidf_vectorizer.pkl')