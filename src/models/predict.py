import joblib

def predict_sentiment(texto):

    modelo = joblib.load('artifacts/sentiment_model.pkl')
    vetorizador = joblib.load('artifacts/tfidf_vectorizer.pkl')
    texto_tfidf = vetorizador.transform([texto])
    previsao = modelo.predict(texto_tfidf)

    return previsao[0]