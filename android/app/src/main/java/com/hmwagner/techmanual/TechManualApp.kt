package com.hmwagner.techmanual

import android.app.Application
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.util.PendingSendStore
import com.hmwagner.techmanual.util.SharedPrefsPendingSendStore

class TechManualApp : Application() {
    override fun onCreate() {
        super.onCreate()
        PendingSendStore.current = SharedPrefsPendingSendStore(this)
        ApiClient.init(this)
    }
}
